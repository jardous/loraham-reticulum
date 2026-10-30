#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Opt-in raw LoRa duplex test via a USB RNode and a Pi over SSH.

Requires explicit --allow-tx. Temporarily uses 867.5 MHz/BW125/SF8/CR4/5/2 dBm
and restores the USB radio settings. Close all applications using the USB port
and the Pi radio before running. The Pi helper exits and sleeps its radio.
"""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import secrets
import shlex
import subprocess
import threading
import time

from LoRaHAMInterface import airtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-tx', action='store_true', required=True)
    parser.add_argument('--port', required=True, help='USB RNode serial device')
    parser.add_argument('--host', required=True, help='SSH destination, e.g. pi@roofnode.local')
    parser.add_argument('--remote-dir', default='loraham-reticulum')
    parser.add_argument('--output', type=Path, help='Optional JSON results file; must not exist')
    args = parser.parse_args()
    if args.host.startswith('-'):
        parser.error('Invalid SSH host')
    if args.output and (args.output.exists() or args.output.is_symlink()):
        parser.error('--output file already exists')
    import serial
    radio = serial.Serial(args.port, 115200, timeout=.05, exclusive=True)
    frames = queue.Queue()
    running = threading.Event()
    running.set()
    metrics = {'rssi': None, 'snr': None}
    errors = []

    def reader():
        active = escaped = False
        frame = bytearray()
        while running.is_set():
            for byte in radio.read(1024):
                if byte == 0xc0:
                    if active and frame:
                        cmd, payload = frame[0], bytes(frame[1:])
                        if cmd == 0x23 and payload:
                            metrics['rssi'] = payload[0] - 157
                        elif cmd == 0x24 and payload:
                            metrics['snr'] = int.from_bytes(payload[:1], 'big', signed=True) / 4
                        elif cmd == 0x90:
                            errors.append(payload.hex())
                            print('USB RADIO ERROR:', payload.hex(), flush=True)
                        frames.put((cmd, payload, metrics['rssi'], metrics['snr']))
                    active, escaped = True, False
                    frame.clear()
                elif active:
                    if escaped:
                        frame.append({0xdc: 0xc0, 0xdd: 0xdb}.get(byte, byte))
                        escaped = False
                    elif byte == 0xdb:
                        escaped = True
                    else:
                        frame.append(byte)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(cmd, data):
        data = data.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc')
        radio.write(b'\xc0' + bytes([cmd]) + data + b'\xc0')
        radio.flush()

    def wait(cmd, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                code, data, rssi, snr = frames.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            if code == cmd:
                return data, rssi, snr
        raise TimeoutError(f'USB response {cmd:#x} timed out')

    def drain():
        while not frames.empty():
            try:
                frames.get_nowait()
            except queue.Empty:
                break

    def request(cmd, data):
        drain()
        send(cmd, data)
        return wait(cmd)[0]

    saved = {}
    changed = False
    remote = None
    events = []
    ready = threading.Event()
    results = []
    try:
        time.sleep(.5)
        if request(8, b'\x73') != b'F':
            raise IOError('Device did not answer RNode detection')
        for cmd in (1, 2, 3, 4, 5, 6):
            saved[cmd] = request(cmd, bytes(4) if cmd in (1, 2) else b'\xff')
        if any(len(value) != (4 if cmd in (1, 2) else 1) for cmd, value in saved.items()):
            raise IOError('Unexpected radio configuration response size')
        # Zero frequency/bandwidth and 0xff power are query sentinels, not
        # values KISS can write back. Refuse to change an unconfigured device.
        if (not int.from_bytes(saved[1], 'big') or not int.from_bytes(saved[2], 'big')
                or saved[3][0] == 255 or not 5 <= saved[4][0] <= 12
                or not 5 <= saved[5][0] <= 8 or saved[6][0] not in (0, 1)):
            raise ValueError('Configure the USB RNode first; its current state cannot be restored safely')
        print('Original USB settings:', {k: int.from_bytes(v, 'big') for k, v in saved.items()}, flush=True)
        changed = True
        for cmd, value in [(6, b'\x00'), (1, (867500000).to_bytes(4, 'big')),
                           (2, (125000).to_bytes(4, 'big')), (3, b'\x02'),
                           (4, b'\x08'), (5, b'\x05'), (6, b'\x01')]:
            if request(cmd, value) != value:
                raise IOError(f'Configure failed: {cmd}')
        token = 'rf-' + secrets.token_hex(4)
        command = shlex.join(['python3', '-u',
                              str(Path(args.remote_dir) / 'radio_echo_test.py'),
                              '--allow-tx', '--token', token, '--seconds', '150'])
        remote = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                   args.host, command], stdout=subprocess.PIPE, text=True)

        def remote_reader():
            for line in remote.stdout:
                print('Pi:', line.strip(), flush=True)
                try:
                    event = json.loads(line)
                    events.append(event)
                    if event.get('event') == 'READY':
                        ready.set()
                except ValueError:
                    pass

        reader_thread = threading.Thread(target=remote_reader, daemon=True)
        reader_thread.start()
        if not ready.wait(15):
            raise TimeoutError('Pi did not become ready')
        next_tx = time.monotonic() + .5
        for size in (32, 200, 450):
            prefix = f'{token}:{size}:'.encode()
            payload = (prefix + bytes(range(256)) * 2)[:size]
            while time.monotonic() < next_tx:
                time.sleep(.1)
            drain()
            send(0, payload)
            chunks = [min(len(payload), 254)]
            if len(payload) > 254:
                chunks.append(len(payload) - 254)
            total_airtime = sum(airtime(n + 1, 125000, 8, 5, 18) for n in chunks)
            next_tx = time.monotonic() + total_airtime * 100 + 1
            print(f'USB sent {size} bytes, sha256={hashlib.sha256(payload).hexdigest()}', flush=True)
            got, rssi, snr = wait(0, timeout=15)
            result = {'size': size, 'matched': got == payload, 'rssi': rssi, 'snr': snr}
            results.append(result)
            print('USB received echo:', result, flush=True)
            if not result['matched']:
                raise IOError('Echo payload mismatch')
        if remote.wait(timeout=10) != 0:
            raise IOError('Pi echo helper failed')
        reader_thread.join(timeout=3)
        if errors:
            raise IOError(f'USB radio errors: {errors}')
        print('RESULT: 32-, 200-, and 450-byte packets passed in both directions', flush=True)
        if args.output:
            with args.output.open('x') as output:
                json.dump({'usb_results': results, 'pi_events': events,
                           'usb_errors': errors}, output, indent=2)
    finally:
        try:
            if changed:
                request(6, b'\x00')
                for cmd in (1, 2, 3, 4, 5):
                    if request(cmd, saved[cmd]) != saved[cmd]:
                        raise IOError(f'USB restore failed: {cmd}')
                if request(6, saved[6]) != saved[6]:
                    raise IOError('USB radio-state restore failed')
                print('USB settings restored and verified', flush=True)
        finally:
            running.clear()
            thread.join(timeout=2)
            radio.close()
            if remote is not None and remote.poll() is None:
                # Let its bounded lifetime and finally block clean up hardware.
                # Killing ssh could leave its remote process holding the radio.
                remote.wait(timeout=160)


if __name__ == '__main__':
    main()
