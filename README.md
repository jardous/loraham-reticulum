# LoRaHAM Reticulum

Use the [LoRaHAM Pi](https://github.com/LoRaHAM/LoRaHAM_Pi)'s SX1276/RFM95 directly
as a [Reticulum](https://reticulum.network/) interface, interoperable over LoRa
with normal RNode firmware. This is a **standalone project**: neither the
original LoRaHAM Pi repository nor patched `pySX127x`, `pyLoRa` or `RPi.GPIO`
is required. It contains its own SPI driver and does not modify other software.

**Status: experimental.** Hardware setup, Reticulum plugin loading, and raw
32-/200-/450-byte packets in both directions have passed on a Raspberry Pi 3.
Authenticated `rnx hostname` execution also passed over LoRa-only profiles,
including path discovery and link establishment. LXMF messaging has not yet
been tested. See [validation.md](validation.md) for the exact test scope.

## Hardware scope and safety

Only the **868 MHz module** is supported. SPI access is hard-coded to
`/dev/spidev0.1` (SPI0 CS1). GPIO numbering is BCM:

| Signal | GPIO | Access |
|---|---:|---|
| CS1 | 7 | Kernel SPI chip select |
| Reset | 6 | Output; resets high-band module only |
| Activity LED | 19 | Output; two startup blinks, RX/TX indication |
| DIO0 | 16 | Not claimed; IRQ registers are polled over SPI |
| DIO1 | 12 | Not claimed |
| DIO2 | 20 | Not claimed |
| DIO3 | 21 | Not claimed |

This agrees with `BOARD("868")` in the refactored `jardous/LoRaHAM_Pi` fork and
the upstream V1.06 board diagram. Confirm your physical board revision
before use. GPIO20 is **not** the activity LED.

CS0 and the 433 MHz reset/LED are never opened or driven. SPI clock, MOSI and
MISO are shared, as designed. Only the interface's own pins are freed on exit;
there is no global GPIO cleanup. Importing the module does not operate hardware.
Initialisation explicitly resets the 868 MHz radio, calibrates its HF image
response, configures LoRa and verifies register readback. Shutdown sleeps it.

**Attach an appropriate 868 MHz antenna before permitting transmission.** Use
frequencies, bandwidth, power and duty cycle permitted in your jurisdiction.
The sample starts receive-only and does not enable transport/routing. No
Internet interfaces, public TCP listeners or system services are installed.

Do not run this alongside Meshtastic, `pySX127x`, or any other process using the
same radio. Its advisory lock prevents two instances of this driver for the
same user; it cannot lock unrelated drivers out of SPI.

## 1. Install dependencies on the Pi

Tested with Raspberry Pi OS/Debian 13, Python 3.13 and RNS 1.5.7. The unit tests
also run without Pi hardware on Python 3.10 and 3.13 in CI.

```sh
sudo apt update
sudo apt install python3-venv python3-pip python3-spidev python3-lgpio
sudo raspi-config nonint do_spi 0
```

Reboot if necessary for SPI, then confirm `/dev/spidev0.1` exists. Your account
must have access to SPI and GPIO; on Raspberry Pi OS:

```sh
sudo usermod -aG spi,gpio "$USER"
```

Log out and back in after changing groups. Do not run the daemon as root just
to work around device permissions.

Clone this repository and create a dedicated virtual environment:

```sh
git clone https://github.com/jardous/loraham-reticulum.git
cd loraham-reticulum
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` declares **rns**, **spidev** and **lgpio**. Using the distro's
SPI/GPIO packages and `--system-site-packages` avoids building native packages
with pip. No `pyLoRa`, `RPi.GPIO`, Meshtastic, LXMF or radio firmware flashing is
required for this interface. Applications wanting LXMF must install it separately.

## 2. Test software and receive-only hardware setup

```sh
.venv/bin/python -m unittest discover -s . -v
.venv/bin/python probe.py --seconds 30
```

The probe drives only the 868 MHz module and never transmits. It should blink
the GPIO19 LED twice and print `version: "0x12"`, `mode: "0x85"` and
`tx_enabled: false`. With no matching transmitter, zero packets is normal.

The LED is software-controlled, **not** an automatic indication of all RF
energy. An RX blink means a CRC-valid physical packet was read, not that
Reticulum authenticated or delivered a message.

## 3. Install and run the Reticulum interface

```sh
.venv/bin/python install.py --config-dir ~/.reticulum-loraham
.venv/bin/rnsd --config ~/.reticulum-loraham --verbose
```

The installer copies `LoRaHAMInterface.py` into the profile's `interfaces/`
directory and `config.example` into its `config`. It **refuses to overwrite**
either file and does not start anything. For an existing profile, manually copy
the module into its `interfaces/` directory and add just the `[[LoRaHAM 868]]`
block under `[interfaces]`; do not replace your other configuration.

In another shell, using the same profile:

```sh
.venv/bin/rnstatus --config ~/.reticulum-loraham
```

Expected: `LoRaHAMInterface[LoRaHAM 868]`, online, approximately **3.125 kb/s**.
The example enables the local shared instance for applications using the same
configuration directory. A different already-running RNS shared instance may
own the default local sharing ports; stop it or use Reticulum's distinct
`shared_instance_port` and `instance_control_port` settings for this profile.

Stop foreground `rnsd` with Ctrl-C. If you change the source file, copy the
updated file to the profile's `interfaces/` directory before restarting.

### Permit transmission deliberately

First confirm hardware/antenna, regulations and matching peer parameters. Then
change **both** switches in `~/.reticulum-loraham/config` and restart:

```ini
outgoing = yes
tx_enabled = yes
```

Neither installation nor importing the module enables TX. `tx_enabled` is a
driver-level guard in addition to Reticulum's `outgoing` option.

### Radio options

| Option | Default | Accepted values |
|---|---:|---|
| `frequency` | `867500000` | 863000000–870000000 Hz; observe local sub-band rules |
| `bandwidth` | `125000` | 125000, 250000, 500000 Hz |
| `spreadingfactor` | `8` | SF7–SF12 |
| `codingrate` | `5` | 5–8, representing 4/5–4/8 |
| `txpower` | `2` | 2–17 dBm through PA_BOOST; no external amplifier |
| `duty_cycle` | `1` | >0 to 1 percent over a rolling hour; permits handshake bursts |
| `tx_enabled` | `no` | Explicit extra TX permission |

Peers must match frequency, bandwidth, SF and coding rate. The driver uses
explicit headers, payload CRC, normal IQ and private sync word `0x12` (SX126x
equivalent `0x1424`). Preamble length is at least 18 symbols, as with RNode.
The reported bitrate is the real modulation bitrate, not an artificially
inflated value.

**SF5 is unsupported on SX127x.** The BW500/SF5 workaround for Retichat 1.33
build 34's low-bitrate outbound bug cannot be used with this HAT. Changing this
interface does not fix that application bug. Use a compatible client/settings.

## 4. Repeat the raw duplex test

This tests LoRa and RNode framing, **not the Reticulum protocol**. No RNS daemon
should be running on either radio. It sends and echoes 32-, 200- and 450-byte
payloads; 450 bytes requires splitting and reassembly on each end.

On the USB host, install Python RNS and pyserial in a virtual environment (RNS
normally installs pyserial). Copy/clone this repository there too. Enable
key-based SSH to the Pi and make sure `radio_echo_test.py` is available remotely.
Close Crosstalk, Sideband and anything else using the USB RNode first.

From `loraham-reticulum/` on the USB host, substituting its Python and serial port:

```sh
python usb_echo_test.py --allow-tx \
  --port /dev/serial/by-id/YOUR_USB_RNODE \
  --host pi@roofnode.local \
  --remote-dir loraham-reticulum \
  --output duplex-results.json
```

The test explicitly transmits at 867.5 MHz/BW125/SF8/CR4/5/2 dBm. It uses a
per-run token, spaces test packets according to airtime, rejects mismatched
echoes, reports RSSI/SNR and restores/readbacks the USB radio settings in its
cleanup path. The Pi helper is limited to three replies and a 150-second
lifetime, then sleeps its radio. Allow roughly two minutes; on failure cleanup
may wait for that bounded remote lifetime. If a device disconnects, inspect its
settings manually—the script cannot guarantee restoration without access.

`--output` refuses to overwrite an existing file. Do not run the echo helper as
a permanent daemon; it is only a bench diagnostic.

## Reticulum protocol integration check

Raw echo success and an online interface are necessary but do not demonstrate
Reticulum routing or message delivery. Authenticated `rnx hostname` has now
passed on radio-only profiles. To repeat a protocol test:

1. Configure a peer's regular `RNodeInterface` for the same radio parameters.
2. Enable `outgoing` and `tx_enabled` on the Pi and start both RNS instances.
3. Run a Reticulum application or destination that announces on the Pi (with
   the same profile directory). Discover that destination on the USB peer.
4. Use `rnprobe --help` to probe its destination hash and correct application
   aspect filter; verify a returned delivery proof over the radio, with no LAN
   or Internet interface providing an alternate path.
5. Test the opposite direction, then LXMF delivery with compatible clients.

### Remote commands with `rnx`

On the Pi, authorise only the PC's **identity hash** (not its destination hash):

```sh
rnx --listen -a PC_IDENTITY_HASH
```

On the PC, target the Pi's **listener destination hash**:

```sh
rnx -w 60 PI_LISTENER_DESTINATION_HASH hostname
rnx -x -w 60 PI_LISTENER_DESTINATION_HASH
```

The second command starts `rnx`'s command prompt. Sending the command `sh` does
not open a persistent SSH-style terminal or allocate a PTY. Each command is an
independent execution; do not use `--noauth` to work around connectivity issues.

For the PC's regular `RNodeInterface`, allow handshake bursts while preserving
the long-term limit:

```ini
airtime_limit_short = 33
airtime_limit_long = 1
```

Setting the short-term limit to 1% can hold control packets too long. Similarly,
the original custom interface's 99×-airtime pause after every packet delayed
path replies and link proofs. The rolling-hour budget replaces that pause:
at 1%, up to 36 seconds of airtime can be used per rolling hour, allowing small
authenticated exchanges to finish without artificial inter-packet delays.

This interface supplies a radio link, not an LXMF mailbox or chat application.
Only enable `enable_transport = yes` if you intentionally want this Pi to route
Reticulum traffic between interfaces.

## Implementation limitations

- A single worker owns SPI; a bounded queue holds at most 32 outgoing packets.
  It polls IRQ flags, reports RSSI/SNR, filters payload CRC errors, applies basic
  signal-detect carrier sensing/random backoff and enforces TX timeouts.
- Packet MTU is 508 bytes using RNode's one-byte header and at most two 254-byte
  fragments. That format has no source or fragment index: lost/interleaved
  fragments or 4-bit sequence collisions can corrupt a reassembly. Upper-layer
  Reticulum processing remains responsible for validating packets.
- Before a packet's first fragment, its total airtime must fit in the remaining
  rolling-hour budget (at most 1%, or 36 seconds/hour). Split fragments are sent
  together and charged at completion. Exhaustion defers the next packet until
  sufficient airtime expires from the window, rather than imposing a pause
  after every packet. This is not a certified regulatory meter or full RNode
  CSMA implementation. Restarting the process does not preserve the budget;
  do not use restarts to evade airtime limits.
- Hardware faults take the interface offline and close its resources. There is
  no automatic reconnect; fix the fault and restart the RNS instance.
- Pins and gpiochip0 are specific to the tested Pi 3 / LoRaHAM Pi mapping.
  Raspberry Pi 5 support is not claimed by this integration.
- The radio probe, echo helper and RNS interface are mutually exclusive. Stop
  one before starting another.

For tests on a non-Pi computer, install only `requirements-test.txt`; fake
GPIO/SPI tests need neither hardware packages nor elevated privileges.

## License

The independently implemented interface and tools in this repository are
MIT-licensed; see [LICENSE](LICENSE). Reticulum and the other dependencies retain
their own licenses. No files from the original `pySX127x` driver are included.
