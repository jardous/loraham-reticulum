# SPDX-License-Identifier: MIT
"""Experimental Reticulum interface for the LoRaHAM Pi V1.06 868 MHz radio.

SPI0/CS1 only. GPIO6 resets the radio and GPIO19 drives its activity LED.
No GPIO or SPI devices are opened when this module is imported.
"""

import fcntl
import math
from pathlib import Path
import queue
import secrets
import threading
import time

import RNS
from RNS.Interfaces.Interface import Interface


def validate_radio(frequency, bandwidth, sf, cr, txpower):
    if not 863000000 <= frequency <= 870000000:
        raise ValueError("This interface supports the 863–870 MHz module only")
    if bandwidth not in (125000, 250000, 500000):
        raise ValueError("Bandwidth must be 125000, 250000 or 500000 Hz")
    if not 7 <= sf <= 12:
        raise ValueError("SX127x explicit-header operation requires SF7–SF12; no SF5")
    if not 5 <= cr <= 8:
        raise ValueError("Coding rate must be 5–8 (4/5–4/8)")
    if not 2 <= txpower <= 17:
        raise ValueError("PA_BOOST TX power must be 2–17 dBm")


def airtime(size, bandwidth, sf, cr, preamble):
    """Seconds on air, explicit header and payload CRC enabled."""
    symbol = 2 ** sf / bandwidth
    ldro = int(symbol > .016)
    symbols = 8 + max(0, math.ceil((8 * size - 4 * sf + 44) /
                                  (4 * (sf - 2 * ldro))) * cr)
    return (preamble + 4.25 + symbols) * symbol


class RNodeFraming:
    """RNode's one-byte radio header and two-fragment (508-byte) framing.

    The wire format has no fragment index or source ID. Like RNode firmware,
    two successive fragments with the same 4-bit sequence are paired. Loss or
    interleaving can produce corrupt packets; Reticulum authenticates them.
    """
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.pending = None

    @staticmethod
    def encode(data, sequence):
        data = bytes(data)
        if not 1 <= len(data) <= 508:
            raise ValueError("Packet must contain 1–508 bytes")
        header = (sequence & 15) << 4
        if len(data) <= 254:
            return [bytes([header]) + data]
        header |= 1
        return [bytes([header]) + data[:254], bytes([header]) + data[254:]]

    def decode(self, frame, now=None):
        now = time.monotonic() if now is None else now
        if not 2 <= len(frame) <= 255:
            return None
        header, payload = frame[0], bytes(frame[1:])
        if header & 0x0e:
            return None
        if not header & 1:
            self.pending = None
            return payload
        sequence = header >> 4
        if self.pending:
            previous, first, when = self.pending
            if previous == sequence and now - when <= self.timeout:
                self.pending = None
                return first + payload
        self.pending = (sequence, payload, now)
        return None


class SX1276Radio:
    """Single-thread-owned, polled SPI driver; never accesses CS0 or GPIO5."""
    def __init__(self, frequency=867500000, bandwidth=125000, sf=8, cr=5,
                 txpower=2):
        validate_radio(frequency, bandwidth, sf, cr, txpower)
        self.bandwidth, self.sf, self.cr = bandwidth, sf, cr
        self.preamble = max(18, math.ceil(.024 * bandwidth / 2 ** sf))
        self.spi = self.gpio = self.lock = None
        self.pins = []
        self.crc_errors = 0
        self.rx_packets = self.tx_packets = 0
        self.led_until = 0
        self.closed = False
        try:
            self._open()
            self._configure(frequency, txpower)
        except Exception:
            self.close()
            raise

    def _open(self):
        import lgpio
        import spidev
        self.lgpio = lgpio
        lockdir = Path.home() / '.cache' / 'loraham'
        lockdir.mkdir(parents=True, exist_ok=True)
        self.lock = (lockdir / 'spi0.1.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.gpio = lgpio.gpiochip_open(0)
        for pin, initial in [(19, 0), (6, 1)]:
            lgpio.gpio_claim_output(self.gpio, pin, initial)
            self.pins.append(pin)
        self.spi = spidev.SpiDev()
        self.spi.open(0, 1)
        self.spi.mode = 0
        self.spi.max_speed_hz = 500000
        # Reset only the high-band radio. Do not call global GPIO cleanup.
        lgpio.gpio_write(self.gpio, 6, 0)
        time.sleep(.01)
        lgpio.gpio_write(self.gpio, 6, 1)
        time.sleep(.15)
        if self.read(0x42) != 0x12:
            raise IOError('No SX1276 detected on /dev/spidev0.1')
        for _ in range(2):
            self.led(True)
            time.sleep(.1)
            self.led(False)
            time.sleep(.1)

    def read(self, address):
        return self.spi.xfer2([address & 0x7f, 0])[1]

    def write(self, address, value):
        self.spi.xfer2([address | 0x80, value])

    def mode(self, value):
        self.write(1, value)
        time.sleep(.005)
        deadline = time.monotonic() + .5
        while self.read(1) != value:
            if time.monotonic() >= deadline:
                raise IOError(f'Radio did not enter mode {value:#04x}')
            time.sleep(.005)

    def led(self, on):
        self.lgpio.gpio_write(self.gpio, 19, int(on))

    def _configure(self, frequency, txpower):
        self.mode(self.read(1) & 0xf8)
        self.mode(0x00)  # FSK sleep, HF band
        frf = round(frequency * (1 << 19) / 32000000)
        for address, value in zip((6, 7, 8), frf.to_bytes(3, 'big')):
            self.write(address, value)
        self.mode(0x01)
        # Image calibration in the selected HF band; timeout is mandatory.
        self.write(0x3b, (self.read(0x3b) & 0xbf) | 0x40)
        deadline = time.monotonic() + 1
        while self.read(0x3b) & 0x20:
            if time.monotonic() >= deadline:
                raise IOError('HF image calibration timed out')
            time.sleep(.005)
        self.mode(0x00)
        self.mode(0x80)
        bw = {125000: 7, 250000: 8, 500000: 9}[self.bandwidth]
        settings = {
            0x09: 0x80 | (txpower - 2),  # PA_BOOST, no external amplifier
            0x0b: 0x2b, 0x0c: 0x23, 0x0e: 0, 0x0f: 0, 0x11: 0,
            0x1d: (bw << 4) | ((self.cr - 4) << 1),
            0x1e: (self.sf << 4) | 4, 0x1f: 100,
            0x20: self.preamble >> 8, 0x21: self.preamble & 255,
            0x23: 255, 0x24: 0,
            0x26: 4 | (8 if 2 ** self.sf / self.bandwidth > .016 else 0),
            0x31: 0xc3, 0x33: 0x27, 0x37: 0x0a, 0x39: 0x12,
            0x3b: 0x1d, 0x40: 0, 0x41: 0, 0x4d: 0x84,
        }
        if self.bandwidth == 500000:
            settings.update({0x36: 0x02, 0x3a: 0x64})  # Semtech HF BW500 erratum
        else:
            settings[0x36] = 0x03
        for address, value in settings.items():
            self.write(address, value)
        for address, value in settings.items():
            # LNA gain bits are controlled by AGC once RegModemConfig3 enables
            # it; verify only the configured boost bits on that register.
            mask = 0x1f if address == 0x0c else 0xff
            actual = self.read(address)
            if actual & mask != value & mask:
                raise IOError(f'Configuration readback failed at {address:#04x}: '
                              f'wrote {value:#04x}, read {actual:#04x}')
        if bytes(self.read(a) for a in (6, 7, 8)) != frf.to_bytes(3, 'big'):
            raise IOError('Frequency readback failed')
        self.receive()

    def receive(self):
        self.mode(0x81)
        self.write(0x0d, 0)
        self.write(0x12, 0xff)
        self.mode(0x85)

    def poll(self):
        if self.led_until and time.monotonic() >= self.led_until:
            self.led(False)
            self.led_until = 0
        flags = self.read(0x12)
        if not flags & 0x40:
            return None
        if flags & 0x20:
            self.crc_errors += 1
            self.write(0x12, 0xff)
            return None
        size = self.read(0x13)
        self.write(0x0d, self.read(0x10))
        payload = bytes(self.spi.xfer2([0] + [0] * size)[1:])
        snr = self.read(0x19)
        snr = (snr if snr < 128 else snr - 256) / 4
        rssi = self.read(0x1a) - 157 + min(0, snr)
        self.write(0x12, 0xff)
        self.rx_packets += 1
        self.led(True)
        self.led_until = time.monotonic() + .1
        return payload, rssi, snr

    def busy(self):
        return bool(self.read(0x18) & 3)

    def time_on_air(self, size):
        return airtime(size, self.bandwidth, self.sf, self.cr, self.preamble)

    def transmit(self, payload):
        if not 1 <= len(payload) <= 255:
            raise ValueError('Physical LoRa packet must contain 1–255 bytes')
        self.mode(0x81)
        try:
            self.led(True)
            self.write(0x0d, 0)
            self.spi.xfer2([0x80] + list(payload))
            self.write(0x22, len(payload))
            self.write(0x12, 0xff)
            self.mode(0x83)
            deadline = time.monotonic() + self.time_on_air(len(payload)) + 1
            while not self.read(0x12) & 8:
                if time.monotonic() >= deadline:
                    raise IOError('LoRa TX completion timed out')
                time.sleep(.002)
            self.tx_packets += 1
        finally:
            self.led(False)
            self.receive()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.spi is not None:
            try:
                self.write(1, 0x80)  # Sleep; never transmit on shutdown
            except Exception:
                pass
            finally:
                try:
                    self.spi.close()
                except Exception:
                    pass
        if self.gpio is not None:
            for pin in self.pins:
                try:
                    self.lgpio.gpio_write(self.gpio, pin, 1 if pin == 6 else 0)
                    self.lgpio.gpio_free(self.gpio, pin)
                except Exception:
                    pass
            self.lgpio.gpiochip_close(self.gpio)
        if self.lock is not None:
            self.lock.close()


class LoRaHAMInterface(Interface):
    DEFAULT_IFAC_SIZE = 8

    def __init__(self, owner, configuration):
        super().__init__()
        config = Interface.get_config_obj(configuration)
        self.name = config['name']
        self.owner = owner
        self.HW_MTU = 508
        self.IN = True
        self.OUT = False  # Reticulum applies the configured outgoing option
        self.shared_medium = True
        self.reports_phy_stats = True
        frequency = int(config.get('frequency', 867500000))
        bandwidth = int(config.get('bandwidth', 125000))
        sf = int(config.get('spreadingfactor', 8))
        cr = int(config.get('codingrate', 5))
        txpower = int(config.get('txpower', 2))
        self.tx_enabled = config.as_bool('tx_enabled') if 'tx_enabled' in config else False
        self.duty_cycle = float(config.get('duty_cycle', 1)) / 100
        if not 0 < self.duty_cycle <= .01:
            raise ValueError('Duty cycle must be >0 and <=1 percent')
        self.bitrate = sf * bandwidth / 2 ** sf * 4 / cr
        self.framing = RNodeFraming()
        self.tx_queue = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.radio = SX1276Radio(frequency, bandwidth, sf, cr, txpower)
        self.online = True
        self.worker = threading.Thread(target=self._run, name='LoRaHAM-868', daemon=True)
        self.worker.start()
        RNS.log(f'{self}: radio initialised, {self.bitrate:.0f} bit/s, '
                f'TX {"enabled" if self.tx_enabled else "disabled"}', RNS.LOG_NOTICE)

    def process_outgoing(self, data):
        if not self.online or not self.tx_enabled or not self.OUT:
            return
        data = bytes(data)
        if not 1 <= len(data) <= self.HW_MTU:
            raise ValueError('LoRaHAM packet exceeds 508-byte MTU')
        try:
            self.tx_queue.put_nowait(data)
        except queue.Full:
            self.tx_drops += 1
            self.tx_dropped_bytes += len(data)
            RNS.log(f'{self}: TX queue full, packet dropped', RNS.LOG_WARNING)

    def _run(self):
        next_tx = time.monotonic() + .1
        pending = None
        try:
            while not self.stop.is_set():
                received = self.radio.poll()
                if received:
                    frame, self.r_stat_rssi, self.r_stat_snr = received
                    data = self.framing.decode(frame)
                    if data is not None:
                        self.rxb += len(data)
                        self.owner.inbound(data, self)
                now = time.monotonic()
                if pending is None:
                    try:
                        data = self.tx_queue.get_nowait()
                        pending = [data, RNodeFraming.encode(data, secrets.randbelow(16)), 0]
                        next_tx = max(next_tx, now + .05 + secrets.randbelow(151) / 1000)
                    except queue.Empty:
                        pass
                if pending is not None and now >= next_tx:
                    if self.radio.busy():
                        next_tx = now + .05 + secrets.randbelow(151) / 1000
                    else:
                        data, frames, index = pending
                        frame = frames[index]
                        self.radio.transmit(frame)
                        # Duty limit applies per whole Reticulum packet. A split
                        # pair must remain contiguous, as on the RNode firmware.
                        index += 1
                        if index == len(frames):
                            self.txb += len(data)
                            total_airtime = sum(self.radio.time_on_air(len(f)) for f in frames)
                            next_tx = time.monotonic() + total_airtime * (1 / self.duty_cycle - 1)
                            pending = None
                        else:
                            pending[2] = index
                            next_tx = time.monotonic()
                self.stop.wait(.005)
        except Exception as error:
            RNS.log(f'{self}: radio worker failed: {error}', RNS.LOG_ERROR)
        finally:
            self.online = False
            self.radio.close()

    def detach(self):
        self.detached = True
        self.online = False
        self.stop.set()
        # Leave hardware owned by its worker until any in-flight TX has ended.
        self.worker.join(timeout=20)

    def __str__(self):
        return f'LoRaHAMInterface[{self.name}]'


interface_class = LoRaHAMInterface
