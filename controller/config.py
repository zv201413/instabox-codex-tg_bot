"""Fixed-machine configuration; runtime secrets also support *_FILE mounts."""
import ipaddress
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def load_env_file(path):
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip(chr(34) + chr(39)))


def secret(name):
    value, filename = os.getenv(name, '').strip(), os.getenv(name + '_FILE', '').strip()
    if value and filename:
        raise ValueError(f'Configure either {name} or {name}_FILE')
    return Path(filename).read_text().strip() if filename else value


def schedule_timezone(name):
    if name == 'UTC':
        return timezone.utc
    match = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', name)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            raise ValueError('Invalid UTC offset')
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(offset if match[1] == '+' else -offset)
    return ZoneInfo(name)


def validate_schedule(start, end, timezone, days):
    for value in (start, end):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise ValueError('Schedule must use HH:MM-HH:MM')
    if start == end:
        raise ValueError('Schedule start and end must differ')
    schedule_timezone(timezone)
    if not days or any(day not in range(7) for day in days):
        raise ValueError('Schedule days must be 0..6 (Monday..Sunday)')


def private_host(host):
    if host == 'localhost':
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return False


@dataclass
class ControllerConfig:
    api_url: str = 'https://api.instabox.sh'
    api_key: str = field(default='', repr=False)
    machine_id: str = ''
    lease_name: str = 'tg-controller'
    lease_ttl: int = 300
    renew_interval: int = 120
    auto_start: bool = False
    keep_mode: str = 'on_demand'
    hold_window_seconds: int = 3600
    max_hold_seconds: int = 21600
    schedule_start: str = '09:00'
    schedule_end: str = '18:00'
    schedule_days: tuple = tuple(range(7))
    schedule_wake: bool = False
    timezone: str = 'Asia/Shanghai'
    tg_token: str = field(default='', repr=False)
    tg_allowed_users: list = field(default_factory=list)
    tg_poll_timeout: int = 25
    gateway_mode: str = 'gateway'
    agent_api_host: str = '127.0.0.1'
    agent_api_port: int = 8080
    agent_secret: str = field(default='', repr=False)
    agent_id: str = 'codex-bridge'
    agent_fresh_seconds: int = 90
    wake_request_seconds: int = 86400
    db_path: str = '/data/controller.db'
    tick_seconds: float = 5

    def validate(self, required=False):
        if not 30 <= self.lease_ttl <= 3600 or not 0 < self.renew_interval < self.lease_ttl:
            raise ValueError('Require 30 <= TTL <= 3600 and 0 < renew < TTL')
        if not 0 < self.tick_seconds < self.lease_ttl - self.renew_interval:
            raise ValueError('Scheduler interval must fit within renewal margin')
        if not 0 < self.hold_window_seconds <= self.max_hold_seconds:
            raise ValueError('Require 0 < HOLD_WINDOW_MINUTES <= MAX_HOLD_MINUTES')
        if self.max_hold_seconds > 8 * 3600:
            raise ValueError('MAX_HOLD_MINUTES must not exceed 480 (8 hours)')
        if self.keep_mode not in ('manual', 'on_demand', 'scheduled'):
            raise ValueError('Invalid keep mode')
        if self.gateway_mode not in ('gateway', 'control_only'):
            raise ValueError('Invalid TELEGRAM_GATEWAY_MODE')
        validate_schedule(self.schedule_start, self.schedule_end, self.timezone, self.schedule_days)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.lease_name):
            raise ValueError('Invalid lease name')
        if not 1 <= self.agent_api_port <= 65535 or not 1 <= self.tg_poll_timeout <= 50:
            raise ValueError('Invalid port or Telegram timeout')
        if self.wake_request_seconds <= 0 or self.agent_fresh_seconds <= 0:
            raise ValueError('Request lifetime and heartbeat freshness must be positive')
        url = urlsplit(self.api_url)
        if url.scheme not in ('https', 'http') or not url.hostname or url.username:
            raise ValueError('Invalid API URL')
        if url.scheme == 'http' and not private_host(url.hostname):
            raise ValueError('Public InstaBox API must use HTTPS')
        if required:
            if not re.fullmatch(r'm_[A-Za-z0-9_-]+', self.machine_id):
                raise ValueError('INSTABOX_MACHINE_ID must be a fixed machine ID')
            for name, value in [('INSTABOX_API_KEY', self.api_key), ('AGENT_API_SECRET', self.agent_secret)]:
                if not value or value.startswith('YOUR_'):
                    raise ValueError(f'{name} is required (or use {name}_FILE)')
            if len(self.agent_secret) < 32:
                raise ValueError('AGENT_API_SECRET must contain at least 32 characters')
            if self.tg_token and (not self.tg_allowed_users or self.tg_token.startswith('YOUR_')):
                raise ValueError('Telegram requires a valid token and nonempty allowed-users list')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.agent_id):
                raise ValueError('Invalid fixed AGENT_ID')
        return self

    @classmethod
    def from_env(cls, env_path=None):
        if env_path:
            load_env_file(env_path)
        flag = lambda key: os.getenv(key, '0').lower() in ('1', 'true', 'yes')
        window = os.getenv('SCHEDULE_WINDOW', '09:00-18:00').split('-')
        if len(window) != 2:
            raise ValueError('Invalid SCHEDULE_WINDOW')
        users = os.getenv('TELEGRAM_ALLOWED_USERS', '').strip()
        if users and not re.fullmatch(r'\d+(?:[,;\s]+\d+)*', users):
            raise ValueError('Invalid TELEGRAM_ALLOWED_USERS')
        ttl = int(os.getenv('INSTABOX_LEASE_TTL', '300'))
        return cls(
            api_url=os.getenv('INSTABOX_API_URL', 'https://api.instabox.sh').rstrip('/'),
            api_key=secret('INSTABOX_API_KEY'), machine_id=os.getenv('INSTABOX_MACHINE_ID', '').strip(),
            lease_name=os.getenv('INSTABOX_LEASE_NAME', 'tg-controller'), lease_ttl=ttl,
            renew_interval=int(os.getenv('INSTABOX_RENEW_EVERY', str(ttl * 2 // 5))),
            auto_start=flag('INSTABOX_AUTO_START'), keep_mode=os.getenv('INSTABOX_KEEP_MODE', 'on_demand'),
            hold_window_seconds=int(os.getenv('HOLD_WINDOW_MINUTES', '60')) * 60,
            max_hold_seconds=int(os.getenv('MAX_HOLD_MINUTES', '360')) * 60,
            schedule_start=window[0].strip(), schedule_end=window[1].strip(),
            schedule_days=tuple(int(day) for day in os.getenv('SCHEDULE_DAYS', '0,1,2,3,4,5,6').split(',')),
            schedule_wake=flag('SCHEDULE_WAKE'), timezone=os.getenv('TZ', 'Asia/Shanghai'),
            tg_token=secret('TELEGRAM_BOT_TOKEN'),
            tg_allowed_users=[int(u) for u in re.split(r'[,;\s]+', users)] if users else [],
            tg_poll_timeout=int(os.getenv('TG_POLL_TIMEOUT', '25')), gateway_mode=os.getenv('TELEGRAM_GATEWAY_MODE', 'gateway'),
            agent_api_host=os.getenv('AGENT_API_HOST', '127.0.0.1'), agent_api_port=int(os.getenv('AGENT_API_PORT', '8080')),
            agent_secret=secret('AGENT_API_SECRET'), agent_id=os.getenv('AGENT_ID', 'codex-bridge'),
            agent_fresh_seconds=int(os.getenv('AGENT_FRESH_SECONDS', '90')),
            wake_request_seconds=int(os.getenv('WAKE_REQUEST_SECONDS', '86400')),
            db_path=os.getenv('CONTROLLER_DB_PATH', '/data/controller.db'), tick_seconds=float(os.getenv('SCHEDULER_INTERVAL', '5')),
        ).validate()
