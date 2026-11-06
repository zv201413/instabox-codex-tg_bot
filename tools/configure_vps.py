#!/usr/bin/env python3
"""Interactive setup: credentials enter protected files, never terminal output."""
import getpass
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.config import ControllerConfig
gateway_spec = importlib.util.spec_from_file_location('setup_gateway', ROOT / 'bridge' / 'codex' / 'gateway.py')
gateway_module = importlib.util.module_from_spec(gateway_spec)
gateway_spec.loader.exec_module(gateway_module)
GatewayClient = gateway_module.GatewayClient


def main():
    import argparse
    from controller.config import schedule_timezone
    from tools.controller_service import default_data_dir
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('process', 'docker'), default='process')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--startup-script', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    data = (args.data_dir or default_data_dir()).expanduser().resolve()
    if args.mode == 'docker' and (args.data_dir or args.startup_script or args.port is not None):
        parser.error('--data-dir, --startup-script and --port apply to process mode; Docker uses container port 8080')
    if args.mode == 'process' and Path('/home/container').is_dir() and not data.is_relative_to(Path('/home/container')):
        parser.error('This container persists /home/container only; keep --data-dir there')
    if args.startup_script:
        from tools.install_startup import place_hook
        original = args.startup_script.read_text() if args.startup_script.exists() else '#!/bin/sh\n'
        place_hook(original, '')  # Reject an ambiguous position before creating credentials.
    target = data / 'controller.env' if args.mode == 'process' else ROOT / 'docker' / '.env.controller'
    connection_file = data / 'bridge-connection.json' if args.mode == 'process' else ROOT / 'bridge-connection.json'
    if target.exists() or connection_file.exists():
        raise ValueError('Configuration already exists; edit it without overwriting live policy')
    machine = input('Machine ID (m_...): ').strip()
    owner = int(input('Telegram user ID: ').strip())
    if owner <= 0:
        raise ValueError('Telegram user ID must be positive')
    url = input('Gateway HTTPS URL or private IP URL (include external port if needed): ').strip().rstrip('/')
    port = args.port if args.port is not None else (int(input('Listener port [8080]: ').strip() or '8080') if args.mode == 'process' else 8080)
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged port in 1024..65535')
    try:
        schedule_timezone('Asia/Shanghai')
        default_zone = 'Asia/Shanghai'
    except Exception:
        default_zone = 'UTC+08:00'
    zone = input(f'Schedule timezone [{default_zone}]: ').strip() or default_zone
    schedule_timezone(zone)
    api_key = getpass.getpass('Instabox API key (hidden): ').strip()
    token = getpass.getpass('Existing conversation bot token (hidden): ').strip()
    if not api_key or not token:
        raise ValueError('Both Instabox API key and existing bot token are required')
    agent_secret = secrets.token_urlsafe(48)
    GatewayClient(url, agent_secret, machine)
    cfg = ControllerConfig(api_key=api_key, machine_id=machine, tg_token=token, tg_allowed_users=[owner],
                           agent_secret=agent_secret, keep_mode='manual', max_hold_seconds=28800,
                           timezone=zone, agent_api_port=port)
    cfg.validate(required=True)
    values = dict(INSTABOX_API_KEY=api_key, INSTABOX_MACHINE_ID=machine, TELEGRAM_BOT_TOKEN=token,
                  TELEGRAM_ALLOWED_USERS=str(owner), AGENT_API_SECRET=agent_secret, AGENT_ID='codex-bridge',
                  TELEGRAM_GATEWAY_MODE='gateway', INSTABOX_KEEP_MODE='manual', HOLD_WINDOW_MINUTES='60',
                  MAX_HOLD_MINUTES='480', INSTABOX_LEASE_NAME='tg-controller', INSTABOX_LEASE_TTL='300',
                  INSTABOX_RENEW_EVERY='120', INSTABOX_AUTO_START='0', SCHEDULE_WAKE='0', TZ=zone)
    if args.mode == 'process':
        values.update(AGENT_API_HOST='0.0.0.0', AGENT_API_PORT=str(port), CONTROLLER_DB_PATH=str(data / 'controller.db'))
    for value in values.values():
        if any(char in value for char in '\r\n$#"\''):
            raise ValueError('Invalid environment-file value')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    try:
        for path, content in [(target, ''.join(f'{key}={value}\n' for key, value in values.items())),
                              (connection_file, json.dumps(dict(url=url, machine_id=machine, secret=agent_secret,
                                                                 agent_id='codex-bridge'), indent=2)+'\n')]:
            with path.open('x') as file:
                created.append(path)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            path.chmod(0o600)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    if args.startup_script:
        from tools.install_startup import install
        install(args.startup_script, ROOT, data)
        print('Startup hook installed; it has not been run.')
    print('Protected configuration created:', target)
    print('Protected Bridge connection file:', connection_file)
    print('Transfer the connection file privately; do not send it in chat.')
    print('Do not start the Controller until the old Telegram poller has stopped.')
    if args.mode == 'process':
        print('Start with tools/controller_service.py start; state and logs stay in the persistent data directory.')
    else:
        print('Build with Docker Compose; start only after stopping the old Telegram poller.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Setup failed:', type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
