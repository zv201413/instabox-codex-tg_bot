"""Process-only configuration, startup hooks, and supervision without privileged paths."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from controller.config import schedule_timezone
from tools import configure_vps, controller_service, install_startup


class SetupTests(unittest.TestCase):
    def test_configure_process_uses_persistent_paths_custom_port_and_private_connection_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'persistent'
            with patch.object(sys, 'argv', ['configure', '--mode', 'process', '--data-dir', str(data), '--port', '18080']), \
                 patch('builtins.input', side_effect=['m_test', '7', 'https://gateway.example.com:18443', 'UTC+08:00']), \
                 patch.object(configure_vps.getpass, 'getpass', side_effect=['test-key', 'test-token']):
                configure_vps.main()
            env = (data / 'controller.env').read_text()
            self.assertIn('AGENT_API_PORT=18080', env)
            self.assertIn(f'CONTROLLER_DB_PATH={data}/controller.db', env)
            self.assertIn('MAX_HOLD_MINUTES=480', env)
            self.assertIn('INSTABOX_KEEP_MODE=manual', env)
            connection = json.loads((data / 'bridge-connection.json').read_text())
            self.assertEqual(connection['url'], 'https://gateway.example.com:18443')
            self.assertGreaterEqual(len(connection['secret']), 32)
            for name in ['controller.env', 'bridge-connection.json']:
                self.assertEqual((data / name).stat().st_mode & 0o777, 0o600)
            with patch.object(sys, 'argv', ['configure', '--data-dir', str(data)]):
                with self.assertRaises(ValueError):
                    configure_vps.main()

    def test_unprivileged_port_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, 'argv', ['configure', '--data-dir', tmp, '--port', '80']), \
                 patch('builtins.input', side_effect=['m_test', '7', 'https://gateway.example.com']):
                with self.assertRaises(ValueError):
                    configure_vps.main()
            self.assertFalse((Path(tmp) / 'controller.env').exists())

    def test_fixed_offset_needs_no_system_timezone_database(self):
        with patch('controller.config.ZoneInfo', side_effect=RuntimeError('tzdata missing')):
            self.assertEqual(schedule_timezone('UTC+08:00').utcoffset(None).total_seconds(), 28800)
            self.assertEqual(schedule_timezone('UTC-05:30').utcoffset(None).total_seconds(), -19800)
            self.assertEqual(schedule_timezone('UTC').utcoffset(None).total_seconds(), 0)
        with self.assertRaises(ValueError):
            schedule_timezone('UTC+24:00')

    def test_startup_hook_precedes_exec_preserves_existing_commands_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'start-all.sh'
            original = '#!/bin/sh\necho old-service\nexec tail -f /dev/null\n'
            path.write_text(original)
            path.chmod(0o750)
            backup = install_startup.install(path, Path(tmp) / 'repo with spaces', Path(tmp) / 'data')
            self.assertEqual(backup.read_text(), original)
            text = path.read_text()
            self.assertLess(text.index(install_startup.BEGIN), text.index('exec tail'))
            self.assertIn('echo old-service', text)
            self.assertIsNone(install_startup.install(path, Path(tmp) / 'repo with spaces', Path(tmp) / 'data'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o750)
            subprocess.run(['sh', '-n', str(path)], check=True)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir()
        self.repo = Path(self.tmp.name) / 'repo'
        (self.repo / 'controller').mkdir(parents=True)
        (self.repo / 'controller' / '__init__.py').touch()
        # The supervised child is a fake Python controller: no platform/TG calls.
        (self.repo / 'controller' / 'main.py').write_text('''import os,time,signal
from pathlib import Path
data=Path(os.environ['TEST_PROCESS_DATA'])
count=data/'child-count'
n=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(n))
if n == 1: raise SystemExit(7)
signal.signal(signal.SIGTERM,lambda *_:exit(0))
while True: time.sleep(0.1)
''')
        (self.data / 'controller.env').write_text('\n'.join([
            'INSTABOX_API_KEY=test-key', 'INSTABOX_MACHINE_ID=m_test', 'AGENT_API_SECRET=' + 's' * 32,
            'TELEGRAM_BOT_TOKEN=test-token', 'TELEGRAM_ALLOWED_USERS=7', 'TZ=UTC',
            f'CONTROLLER_DB_PATH={self.data}/controller.db']) + '\n')
        self.script = Path(controller_service.__file__)

    def tearDown(self):
        self.call('stop')
        self.tmp.cleanup()

    def call(self, action):
        env = dict(os.environ, TEST_PROCESS_DATA=str(self.data))
        return subprocess.run([sys.executable, str(self.script), action, '--repo', str(self.repo),
                               '--data-dir', str(self.data)], env=env, capture_output=True, text=True, timeout=15)

    def test_restart_after_child_exit_duplicate_start_and_graceful_stop(self):
        self.assertEqual(self.call('start').returncode, 0)
        pid = controller_service.running(self.data)
        self.assertIsNotNone(pid)
        self.assertEqual(self.call('start').returncode, 0)
        self.assertEqual(controller_service.running(self.data), pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.data / 'child-count').exists() and int((self.data / 'child-count').read_text()) >= 2:
                break
            time.sleep(0.1)
        self.assertEqual(int((self.data / 'child-count').read_text()), 2)
        self.assertEqual(self.call('status').returncode, 0)
        self.assertEqual(self.call('stop').returncode, 0)
        self.assertIsNone(controller_service.running(self.data))
        self.assertTrue((self.data / 'controller.env').is_file())

    def test_stale_pid_record_is_not_treated_as_our_process(self):
        (self.data / 'supervisor.json').write_text(json.dumps(dict(pid=os.getpid(), identity='wrong-start-time')))
        self.assertIsNone(controller_service.running(self.data))
        self.assertEqual(self.call('stop').returncode, 0)


if __name__ == '__main__':
    unittest.main()
