#!/usr/bin/env python3
"""Install source without restarting; optionally merge gateway configuration."""
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1] / 'bridge' / 'codex'
gateway_spec = importlib.util.spec_from_file_location('installer_gateway', SOURCE / 'gateway.py')
gateway_module = importlib.util.module_from_spec(gateway_spec)
gateway_spec.loader.exec_module(gateway_module)
GatewayClient = gateway_module.GatewayClient


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(runtime, connection=None):
    runtime = Path(runtime)
    config_path = runtime / 'secrets.json'
    if not config_path.is_file():
        raise ValueError('Existing Bridge configuration is required')
    config = json.loads(config_path.read_text())
    if connection is not None:
        GatewayClient(connection['url'], connection['secret'], connection['machine_id'],
                      connection.get('agent_id', 'codex-bridge'))
        config['message_source'] = 'gateway'
        config['gateway'] = connection
    backup = runtime / 'backups' / f'single-bot-{time.time_ns()}'
    backup.mkdir(parents=True, mode=0o700)
    for name in ['bridge.py','elicitation.py','gateway.py','secrets.json']:
        path = runtime / name
        if path.is_file():
            shutil.copy2(path, backup / name)
            (backup / name).chmod(0o600)
    state = runtime / 'state.sqlite3'
    if state.is_file():
        with sqlite3.connect(f'file:{state}?mode=ro', uri=True) as old:
            with sqlite3.connect(backup / 'state.sqlite3') as saved:
                old.backup(saved)
        (backup / 'state.sqlite3').chmod(0o600)
    for name in ['gateway.py','elicitation.py','bridge.py']:
        atomic_write(runtime / name, (SOURCE / name).read_bytes())
    if connection is not None:
        atomic_write(config_path, (json.dumps(config, ensure_ascii=False, indent=2)+'\n').encode())
    return backup


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', default='/data/home/telegram-codex')
    parser.add_argument('--connection-file', help='Protected JSON file created by VPS setup')
    parser.add_argument('--gateway-url')
    parser.add_argument('--machine-id')
    parser.add_argument('--secret-file', help='Read shared agent secret from a protected file')
    args = parser.parse_args()
    if args.connection_file and (args.gateway_url or args.machine_id or args.secret_file):
        parser.error('Use a connection file or separate gateway arguments')
    connection = None
    if args.connection_file:
        connection = json.loads(Path(args.connection_file).read_text())
    elif args.gateway_url:
        if not args.machine_id:
            parser.error('--machine-id is required with --gateway-url')
        secret = Path(args.secret_file).read_text().strip() if args.secret_file else getpass.getpass('Agent shared secret (hidden): ')
        connection = dict(url=args.gateway_url, machine_id=args.machine_id, secret=secret, agent_id='codex-bridge')
    elif args.machine_id or args.secret_file:
        parser.error('--gateway-url is required')
    backup = install(args.runtime, connection)
    print('Bridge 1.2.0 source installed; existing state and configuration retained.')
    print('Backup:', backup)
    print('Gateway configuration merged.' if connection else 'Message source unchanged.')
    print('Service was not restarted. Switch only after VPS configuration is ready.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Install failed:', type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
