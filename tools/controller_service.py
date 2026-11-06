#!/usr/bin/env python3
"""Unprivileged process supervision with persistent files and PID identity checks."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.config import ControllerConfig


def default_data_dir():
    base = Path('/home/container') if Path('/home/container').is_dir() else Path.home()
    return base / '.tg_bridge' / 'instabox-controller'


def process_identity(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def running(data):
    try:
        state = json.loads((data / 'supervisor.json').read_text())
        pid = int(state['pid'])
        return pid if process_identity(pid) == state['identity'] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_state(data):
    fd, name = tempfile.mkstemp(prefix='.supervisor-', dir=data)
    with os.fdopen(fd, 'w') as file:
        json.dump(dict(pid=os.getpid(), identity=process_identity(os.getpid())), file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(name, data / 'supervisor.json')


def validate(data):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    env = data / 'controller.env'
    if not env.is_file():
        raise ValueError('Run tools/configure_vps.py --mode process first')
    keys = {line.split('=', 1)[0].strip() for line in env.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#') and '=' in line}
    for key in keys:
        os.environ.pop(key, None)
    cfg = ControllerConfig.from_env(env).validate(required=True)
    if not Path(cfg.db_path).resolve().is_relative_to(data):
        raise ValueError('CONTROLLER_DB_PATH must be inside the persistent data directory')
    return env


def supervise(repo, data):
    os.umask(0o077)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = validate(data)
    with open(data / 'supervisor.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        save_state(data)
        stopped, child = False, None

        def stop(_signum, _frame):
            nonlocal stopped
            stopped = True
            if child and child.poll() is None:
                child.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        delay = 2
        try:
            while not stopped:
                started = time.monotonic()
                keys = {line.split('=', 1)[0].strip() for line in env.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith('#') and '=' in line}
                child_env = {key: value for key, value in os.environ.items() if key not in keys}
                child = subprocess.Popen([sys.executable, '-u', '-m', 'controller.main', '--env', str(env)], cwd=repo, env=child_env)
                rotated = time.monotonic()
                while child.poll() is None and not stopped:
                    if time.monotonic() - rotated > 5:
                        log = data / 'controller.log'
                        if log.is_file() and log.stat().st_size > 10 * 1024 * 1024:
                            shutil.copyfile(log, data / 'controller.log.1')
                            (data / 'controller.log.1').chmod(0o600)
                            with log.open('wb'):
                                pass
                        rotated = time.monotonic()
                    time.sleep(0.2)
                if stopped:
                    try:
                        child.wait(timeout=80)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    break
                print(f'Controller exited ({child.returncode}); retrying in {delay}s', flush=True)
                until = time.monotonic() + delay
                while not stopped and time.monotonic() < until:
                    time.sleep(0.2)
                delay = 2 if time.monotonic() - started > 60 else min(30, delay * 2)
        finally:
            # Only this supervisor owns the record while holding the lock.
            (data / 'supervisor.json').unlink(missing_ok=True)
        return 0


def start(repo, data):
    os.umask(0o077)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(data / 'start.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = running(data)
        if pid:
            print(f'Controller supervisor already running: {pid}')
            return 0
        validate(data)
        log = data / 'controller.log'
        # Rotate on explicit startup; the persistent log never enters the repository.
        if log.exists() and log.stat().st_size > 10 * 1024 * 1024:
            os.replace(log, data / 'controller.log.1')
        with open(log, 'ab', buffering=0) as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run', '--repo', str(repo),
                                        '--data-dir', str(data)], stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=output, start_new_session=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if running(data) == process.pid:
                print(f'Controller supervisor started: {process.pid}; log: {log}')
                return 0
            if process.poll() is not None:
                raise RuntimeError('Supervisor startup failed; inspect its protected log')
            time.sleep(0.1)
        raise RuntimeError('Supervisor startup timed out; inspect its protected log')


def stop(data):
    pid = running(data)
    if not pid:
        print('Controller supervisor is not running')
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 85
    while running(data) == pid and time.monotonic() < deadline:
        time.sleep(0.2)
    if running(data) == pid:
        raise RuntimeError('Graceful stop still pending; inspect the supervisor log')
    print('Controller supervisor stopped; configuration and SQLite retained')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'status', 'run'))
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    args = parser.parse_args()
    repo, data = args.repo.resolve(), args.data_dir.expanduser().resolve()
    if args.action == 'start':
        return start(repo, data)
    if args.action == 'stop':
        return stop(data)
    if args.action == 'run':
        return supervise(repo, data)
    pid = running(data)
    print(f'Controller supervisor running: {pid}' if pid else 'Controller supervisor is not running')
    return 0 if pid else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Service operation failed:', type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
