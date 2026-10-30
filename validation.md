# LoRaHAM Reticulum hardware validation

Test date: 2026-10-30. These results describe the tested implementation and
setup, not a claim about every board revision or radio configuration.

## Setup

- LoRaHAM Pi 868 MHz module on Raspberry Pi 3 Model B, Debian 13, Python 3.13.
- RNS 1.5.7; spidev 3.6; lgpio 0.2.2.0.
- Peer: Seeed XIAO ESP32S3 + SX1262, RNode firmware 1.86, USB/KISS.
- Only SPI0 CS1, reset GPIO6 and LED GPIO19 were operated. The 433 MHz module
  was not opened, reset or configured.
- Both radios: 867.5 MHz, BW125 kHz, SF8, CR4/5, explicit header, CRC,
  private sync word, normal IQ, 2 dBm TX power.

## Results

| Check | Result |
|---|---|
| SPI detection | Version register `0x42` consistently returned `0x12` |
| Hardware setup | Reset, HF calibration, configuration readback, RX mode `0x85` |
| RNS custom module loading | Interface online at 3,125 bit/s, both TX guards off |
| Three short USB → Pi packets | 3/3 exact matches, valid CRC |
| 32-byte USB → Pi → USB echo | Exact byte and SHA-256 match |
| 200-byte USB → Pi → USB echo | Exact byte and SHA-256 match |
| 450-byte USB → Pi → USB echo | Exact match; two LoRa frames each direction |
| Duplex frame totals on Pi | Four RX, four TX, zero CRC errors |
| Actual Reticulum announces/links/LXMF | **Not yet tested over air** |

Duplex signal readings:

| Payload | Pi RX RSSI / SNR | USB RX RSSI / SNR |
|---:|---|---|
| 32 bytes | −59 dBm / 11.5 dB | −83 dBm / 12.5 dB |
| 200 bytes | −60 dBm / 12 dB | −83 dBm / 12 dB |
| 450 bytes | −60 dBm / 12.5 dB | −83 dBm / 12.25 dB |

These are driver-reported measurements, not calibrated lab measurements. USB
settings were restored and verified after the tests. The Pi test process exited
and the 868 MHz module was left in LoRa sleep (`0x80`). No systemd service was
enabled, and no changes were needed to the existing `pySX127x` files.

## Initialisation findings

An earlier SPI-only receiver entered RX mode but decoded none of the test
packets. This driver's explicit GPIO6 reset and HF calibration sequence was
followed by successful reception and duplex operation. This shows that the
initialisation sequence matters; it does not isolate a single causal register
or step.

When automatic gain control is enabled, LNA gain bits change independently.
Readback validation must check the configured boost bits rather than treating
the AGC-controlled gain bits as a failed write.

The indicator LED is controlled in software. Lack of blinking in a direct SPI
probe that never drives GPIO19 is not evidence of a defective radio.
