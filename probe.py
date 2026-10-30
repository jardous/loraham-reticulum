#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Receive-only hardware check using the same driver as the RNS interface."""
import argparse
import json
import math
import time
from LoRaHAMInterface import SX1276Radio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('--seconds must be finite and positive')
    radio = SX1276Radio()
    try:
        print(json.dumps({'ready': True, 'version': hex(radio.read(0x42)),
                          'mode': hex(radio.read(1)), 'tx_enabled': False}), flush=True)
        deadline = time.monotonic() + args.seconds
        status = 0
        while time.monotonic() < deadline:
            status |= radio.read(0x18)
            received = radio.poll()
            if received:
                data, rssi, snr = received
                print(json.dumps({'payload': data.hex(), 'rssi': rssi, 'snr': snr}), flush=True)
            time.sleep(.005)
        print(json.dumps({'rx_packets': radio.rx_packets, 'crc_errors': radio.crc_errors,
                          'tx_packets': radio.tx_packets, 'status_seen': hex(status)}), flush=True)
    finally:
        radio.close()


if __name__ == '__main__':
    main()
