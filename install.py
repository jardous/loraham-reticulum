#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install a receive-only Reticulum profile without overwriting existing files.

This only copies files. It never opens hardware, installs dependencies, starts
services or enables transmission. See README.md for the separate setup steps.
"""
import argparse
from pathlib import Path


def install(config_dir):
    source = Path(__file__).resolve().parent
    config_dir = Path(config_dir).expanduser()
    targets = [(source / 'LoRaHAMInterface.py', config_dir / 'interfaces' / 'LoRaHAMInterface.py'),
               (source / 'config.example', config_dir / 'config')]
    for _, target in targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f'Refusing to overwrite {target}')
    for original, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x') as destination:
            destination.write(original.read_text())
    return config_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', default='~/.reticulum-loraham')
    args = parser.parse_args()
    try:
        target = install(args.config_dir)
    except FileExistsError as error:
        parser.exit(1, f'{error}\nChoose a new profile or update existing files manually.\n')
    print(f'Installed receive-only profile at {target}')
    print('No service was started. Follow README.md to run and test the interface.')


if __name__ == '__main__':
    main()
