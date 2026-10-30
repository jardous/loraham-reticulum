# SPDX-License-Identifier: MIT
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import LoRaHAMInterface as module
import install


class FramingTests(unittest.TestCase):
    def test_roundtrip_boundaries(self):
        for size in [1, 32, 200, 254, 255, 450, 500, 508]:
            for sequence in range(16):
                with self.subTest(size=size, sequence=sequence):
                    data = bytes(i % 256 for i in range(size))
                    decoder = module.RNodeFraming()
                    frames = decoder.encode(data, sequence)
                    self.assertTrue(all(len(f) <= 255 for f in frames))
                    if len(frames) == 2:
                        self.assertIsNone(decoder.decode(frames[0], now=1))
                        result = decoder.decode(frames[1], now=2)
                    else:
                        result = decoder.decode(frames[0], now=1)
                    self.assertEqual(result, data)

    def test_rnode_wire_header(self):
        self.assertEqual(module.RNodeFraming.encode(b'hello', 10), [b'\xa0hello'])
        self.assertEqual(module.RNodeFraming.encode(b'x' * 255, 2),
                         [b'\x21' + b'x' * 254, b'\x21x'])

    def test_bad_sizes(self):
        for data in [b'', b'x' * 509]:
            with self.assertRaises(ValueError):
                module.RNodeFraming.encode(data, 0)

    def test_timeout_and_interleaving(self):
        d = module.RNodeFraming(timeout=3)
        self.assertIsNone(d.decode(b'\x11first', now=0))
        self.assertIsNone(d.decode(b'\x11late', now=4))
        self.assertIsNone(d.decode(b'\x21new', now=5))
        self.assertEqual(d.decode(b'\x21last', now=6), b'newlast')
        d.decode(b'\x11old', now=7)
        self.assertEqual(d.decode(b'\x30single', now=8), b'single')
        self.assertIsNone(d.pending)

    def test_invalid_physical_packets(self):
        d = module.RNodeFraming()
        for frame in [b'', b'\x10', b'\x02bad', b'x' * 256]:
            self.assertIsNone(d.decode(frame))


class AirtimeBudgetTests(unittest.TestCase):
    def test_handshake_burst_has_no_per_packet_pause(self):
        budget = module.AirtimeBudget(.01)
        for when in range(10):
            self.assertEqual(budget.ready_at(.5, when), when)
            budget.charge(.5, when + .5)

    def test_exhaustion_and_rolling_release(self):
        budget = module.AirtimeBudget(.01)
        budget.charge(20, 10)
        budget.charge(15, 20)
        self.assertEqual(budget.ready_at(1, 30), 30)
        self.assertEqual(budget.ready_at(2, 30), 3610)
        self.assertEqual(budget.ready_at(22, 30), 3620)
        self.assertEqual(budget.ready_at(2, 3610), 3610)
        self.assertEqual(len(budget.history), 1)

    def test_oversized_airtime_rejected(self):
        budget = module.AirtimeBudget(.001)
        for airtime in [0, -1, 4, float('nan'), float('inf')]:
            with self.subTest(airtime=airtime), self.assertRaises(ValueError):
                budget.ready_at(airtime, 0)


class ParameterTests(unittest.TestCase):
    def test_reject_433_and_sf5(self):
        for args in [(433000000, 125000, 8, 5, 2),
                     (878000000, 125000, 8, 5, 2),
                     (867500000, 125000, 5, 5, 2),
                     (867500000, 125000, 6, 5, 2),
                     (867500000, 123456, 8, 5, 2),
                     (867500000, 125000, 8, 4, 2),
                     (867500000, 125000, 8, 5, 18)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                module.validate_radio(*args)

    def test_airtime(self):
        # Semtech equation: 20 bytes, SF7/BW125/CR4/5, CRC, preamble8.
        self.assertAlmostEqual(module.airtime(20, 125000, 7, 5, 8), .056576)
        self.assertGreater(module.airtime(255, 125000, 12, 8, 18), 10)


class FakeSPI:
    def __init__(self):
        self.regs = {1: 9, 0x42: 0x12, 0x3b: 2}
        self.operations = []
        self.opened = None
        self.closed = False
        self.fifo = b''

    def open(self, bus, cs):
        self.opened = (bus, cs)

    def close(self):
        self.closed = True

    def xfer2(self, data):
        self.operations.append(tuple(data))
        addr = data[0] & 127
        if data[0] & 128:
            if addr == 0:
                self.fifo = bytes(data[1:])
            else:
                for i, value in enumerate(data[1:]):
                    register = addr + i
                    if register == 0x12:
                        self.regs[register] = self.regs.get(register, 0) & ~value
                    else:
                        self.regs[register] = value
                if addr == 1 and data[1] == 0x83:
                    self.regs[0x12] = 8
                if addr == 0x26 and data[1] & 4:
                    self.regs[0x0c] &= 0x1f  # AGC takes control of gain bits
            return [0] * len(data)
        if addr == 0:
            return [0] + list(self.fifo[:len(data) - 1])
        return [0] + [self.regs.get(addr + i, 0) for i in range(len(data) - 1)]


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spi = FakeSPI()
        self.gpio = Mock()
        self.gpio.gpiochip_open.return_value = 10
        self.patches = [
            patch.dict(sys.modules, {'spidev': SimpleNamespace(SpiDev=lambda: self.spi),
                                     'lgpio': self.gpio}),
            patch.object(Path, 'home', return_value=Path(self.temp.name)),
            patch.object(module.time, 'sleep'),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_only_868_hardware_used_and_no_startup_tx(self):
        r = module.SX1276Radio()
        self.assertEqual(self.spi.opened, (0, 1))
        claimed = [call.args[1] for call in self.gpio.gpio_claim_output.call_args_list]
        self.assertEqual(claimed, [19, 6])
        self.assertEqual(self.spi.regs[0x1d], 0x72)
        self.assertEqual(self.spi.regs[0x1e], 0x84)
        self.assertEqual(self.spi.regs[0x39], 0x12)
        self.assertEqual(self.spi.regs[1], 0x85)
        self.assertNotIn((0x81, 0x83), self.spi.operations)
        r.close()
        r.close()
        freed = [call.args[1] for call in self.gpio.gpio_free.call_args_list]
        self.assertEqual(freed, [19, 6])
        self.assertTrue(self.spi.closed)

    def test_bad_chip_releases_resources(self):
        self.spi.regs[0x42] = 0xff
        with self.assertRaises(IOError):
            module.SX1276Radio()
        self.assertTrue(self.spi.closed)
        self.gpio.gpiochip_close.assert_called_once_with(10)

    def test_rx_crc_and_signed_snr(self):
        r = module.SX1276Radio()
        self.addCleanup(r.close)
        self.spi.regs.update({0x12: 0x60})
        self.assertIsNone(r.poll())
        self.assertEqual(r.crc_errors, 1)
        self.spi.fifo = b'\x10hello'
        self.spi.regs.update({0x12: 0x40, 0x13: 6, 0x10: 0, 0x19: 0xf0, 0x1a: 67})
        self.assertEqual(r.poll(), (b'\x10hello', -94, -4))

    def test_tx_and_return_to_rx(self):
        r = module.SX1276Radio()
        self.addCleanup(r.close)
        r.transmit(b'\x10hello')
        self.assertEqual(r.tx_packets, 1)
        self.assertEqual(self.spi.regs[1], 0x85)
        self.assertEqual(self.spi.fifo, b'\x10hello')

    def test_tx_timeout_returns_to_rx(self):
        r = module.SX1276Radio()
        self.addCleanup(r.close)
        transfer = self.spi.xfer2
        def no_completion(data):
            result = transfer(data)
            if tuple(data) == (0x81, 0x83):
                self.spi.regs[0x12] = 0
            return result
        self.spi.xfer2 = no_completion
        with patch.object(module.time, 'monotonic', side_effect=range(100)):
            with self.assertRaisesRegex(IOError, 'TX completion timed out'):
                r.transmit(b'\x10hello')
        self.assertEqual(self.spi.regs[1], 0x85)

    def test_ldro_and_bw500_errata(self):
        r = module.SX1276Radio(sf=12)
        self.assertEqual(self.spi.regs[0x26], 12)
        r.close()
        r = module.SX1276Radio(bandwidth=500000)
        self.addCleanup(r.close)
        self.assertEqual(self.spi.regs[0x36], 2)
        self.assertEqual(self.spi.regs[0x3a], 0x64)


class InterfaceTests(unittest.TestCase):
    def make_interface(self, **config):
        def base_init(instance):
            instance.tx_drops = instance.tx_dropped_bytes = 0
            instance.rxb = instance.txb = 0
        with patch.object(module.Interface, '__init__', base_init), \
             patch.object(module, 'SX1276Radio'), \
             patch.object(module.threading, 'Thread'), \
             patch.object(module.RNS, 'log'):
            interface = module.LoRaHAMInterface(Mock(), {'name': 'Test', **config})
        return interface

    def test_tx_requires_two_deliberate_switches(self):
        i = self.make_interface()
        i.OUT = True
        i.process_outgoing(b'hello')
        self.assertTrue(i.tx_queue.empty())
        i = self.make_interface(tx_enabled='yes')
        i.process_outgoing(b'hello')
        self.assertTrue(i.tx_queue.empty())
        i.OUT = True
        i.process_outgoing(b'hello')
        self.assertEqual(i.tx_queue.get_nowait(), b'hello')

    def test_queue_bounded(self):
        i = self.make_interface(tx_enabled='yes')
        i.OUT = True
        with patch.object(module.RNS, 'log'):
            for _ in range(33):
                i.process_outgoing(b'hello')
        self.assertEqual(i.tx_queue.qsize(), 32)
        self.assertEqual(i.tx_drops, 1)

    def test_limits(self):
        for value in ['0', '-1', '1.01', 'nan', 'inf']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make_interface(duty_cycle=value)

    def test_worker_reassembles_before_delivering(self):
        i = self.make_interface()
        payload = b'x' * 450
        frames = module.RNodeFraming.encode(payload, 3)
        i.radio.poll.side_effect = [(f, -70, 10) for f in frames] + [None]
        i.stop.is_set = Mock(side_effect=[False, False, False, True])
        i._run()
        i.owner.inbound.assert_called_once_with(payload, i)
        self.assertEqual(i.rxb, 450)
        i.radio.close.assert_called_once()

    def test_worker_splits_and_counts_tx(self):
        i = self.make_interface(tx_enabled='yes')
        i.OUT = True
        payload = b'x' * 450
        i.process_outgoing(payload)
        i.radio.poll.return_value = None
        i.radio.busy.return_value = False
        i.radio.time_on_air.return_value = .01
        i.stop.is_set = Mock(side_effect=[False] * 5 + [True])
        with patch.object(module.time, 'monotonic', side_effect=range(100)):
            i._run()
        self.assertEqual(i.txb, 450)
        sent = [call.args[0] for call in i.radio.transmit.call_args_list]
        self.assertEqual([len(f) for f in sent], [255, 197])
        self.assertEqual(sent[0][1:] + sent[1][1:], payload)
        self.assertEqual(sent[0][0], sent[1][0])

    def test_worker_stays_offline_on_fault(self):
        i = self.make_interface()
        i.radio.poll.side_effect = IOError('SPI failure')
        with patch.object(module.RNS, 'log'):
            i._run()
        self.assertFalse(i.online)
        i.radio.close.assert_called_once()

    def test_worker_allows_handshake_packets_in_burst(self):
        i = self.make_interface(tx_enabled='yes')
        i.OUT = True
        for _ in range(3):
            i.process_outgoing(b'handshake')
        i.radio.poll.return_value = None
        i.radio.busy.return_value = False
        i.radio.time_on_air.return_value = .5
        i.stop.is_set = Mock(side_effect=[False] * 12 + [True])
        with patch.object(module.time, 'monotonic', side_effect=range(100)):
            i._run()
        self.assertEqual(i.radio.transmit.call_count, 3)
        self.assertEqual(len(i.airtime_budget.history), 3)

    def test_worker_waits_for_budget_without_transmitting(self):
        i = self.make_interface(tx_enabled='yes')
        i.OUT = True
        i.process_outgoing(b'handshake')
        i.airtime_budget.charge(36, 0)
        i.radio.poll.return_value = None
        i.radio.busy.return_value = False
        i.radio.time_on_air.return_value = .5
        i.stop.is_set = Mock(side_effect=[False] * 12 + [True])
        with patch.object(module.time, 'monotonic', side_effect=range(100)):
            i._run()
        i.radio.transmit.assert_not_called()

    def test_detach(self):
        i = self.make_interface()
        i.detach()
        self.assertTrue(i.stop.is_set())
        self.assertFalse(i.online)
        i.worker.join.assert_called_once()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / 'profile'

    def test_installs_receive_only_profile(self):
        install.install(self.target)
        config = module.Interface.get_config_obj(str(self.target / 'config'))
        interface = config['interfaces']['LoRaHAM 868']
        self.assertFalse(interface.as_bool('outgoing'))
        self.assertFalse(interface.as_bool('tx_enabled'))
        installed = self.target / 'interfaces' / 'LoRaHAMInterface.py'
        self.assertEqual(installed.read_text(), Path(module.__file__).read_text())

    def test_refuses_existing_config_without_writing_interface(self):
        self.target.mkdir()
        (self.target / 'config').write_text('user configuration')
        with self.assertRaises(FileExistsError):
            install.install(self.target)
        self.assertEqual((self.target / 'config').read_text(), 'user configuration')
        self.assertFalse((self.target / 'interfaces').exists())

    def test_refuses_dangling_config_symlink(self):
        self.target.mkdir()
        (self.target / 'config').symlink_to(self.target / 'missing')
        with self.assertRaises(FileExistsError):
            install.install(self.target)
        self.assertFalse((self.target / 'missing').exists())

    def test_refuses_existing_interface_without_writing_config(self):
        target = self.target / 'interfaces' / 'LoRaHAMInterface.py'
        target.parent.mkdir(parents=True)
        target.write_text('user interface')
        with self.assertRaises(FileExistsError):
            install.install(self.target)
        self.assertEqual(target.read_text(), 'user interface')
        self.assertFalse((self.target / 'config').exists())

    def test_external_module_load_has_no_hardware_side_effects(self):
        namespace = {'Interface': module.Interface, 'RNS': module.RNS}
        gpio = Mock()
        spi = Mock()
        with patch.dict(sys.modules, {'spidev': spi, 'lgpio': gpio}):
            exec(Path(module.__file__).read_text(), namespace)
        self.assertTrue(issubclass(namespace['interface_class'], module.Interface))
        spi.SpiDev.assert_not_called()
        gpio.gpiochip_open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
