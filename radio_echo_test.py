#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Temporary raw-LoRa echo test, not an RNS daemon. TX requires --allow-tx.

Accepts only payloads prefixed with a per-run token. Sends at most three replies
and spaces whole packets by their airtime / 1% duty cycle. All hardware access
uses the project's 868-only driver.
"""
import argparse
import hashlib
import json
import math
import secrets
import time

from LoRaHAMInterface import SX1276Radio, RNodeFraming


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-tx', action='store_true', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args()
    try:
        prefix = args.token.encode('ascii') + b':'
    except UnicodeEncodeError:
        parser.error('Token must be ASCII')
    if len(prefix) > 24 or len(prefix) < 2:
        parser.error('Token must contain 1–23 ASCII bytes')
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 600:
        parser.error('--seconds must be finite, positive and at most 600')
    radio = SX1276Radio()
    framing = RNodeFraming()
    next_tx = 0
    pending = None
    completed = set()
    replies = 0
    try:
        emit('READY', token=args.token, mode=hex(radio.read(1)))
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and replies < 3:
            got = radio.poll()
            if got:
                frame, rssi, snr = got
                payload = framing.decode(frame)
                if payload is not None and payload.startswith(prefix):
                    digest = hashlib.sha256(payload).hexdigest()
                    emit('RECEIVED', size=len(payload), sha256=digest, rssi=rssi, snr=snr)
                    if digest not in completed and pending is None:
                        pending = (payload, digest)
            if pending and time.monotonic() >= next_tx and not radio.busy():
                payload, digest = pending
                frames = framing.encode(payload, secrets.randbelow(16))
                for frame in frames:
                    radio.transmit(frame)
                total_airtime = sum(radio.time_on_air(len(f)) for f in frames)
                next_tx = time.monotonic() + total_airtime * 99
                completed.add(digest)
                replies += 1
                pending = None
                emit('ECHOED', size=len(payload), frames=len(frames),
                     sha256=digest, airtime=total_airtime)
            time.sleep(.005)
        emit('SUMMARY', replies=replies, rx_frames=radio.rx_packets,
             tx_frames=radio.tx_packets, crc_errors=radio.crc_errors)
    finally:
        radio.close()
        emit('CLOSED')


if __name__ == '__main__':
    main()
