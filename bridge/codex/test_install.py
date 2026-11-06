import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('install_single_bridge', Path(__file__).resolve().parents[2]/'tools'/'install_bridge.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_install_preserves_runtime_configuration_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            original=dict(telegram_token='original-token',pairing_code='original-code',codex_environment={'MODEL_KEY':'original'})
            config=json.dumps(original)
            (root/'secrets.json').write_text(config)
            (root/'bridge.py').write_text('old source')
            db=sqlite3.connect(root/'state.sqlite3')
            db.execute('CREATE TABLE state(key TEXT, value TEXT)')
            db.execute("INSERT INTO state VALUES('thread_id','existing-thread')")
            db.commit()
            backup=installer.install(root)
            self.assertEqual((root/'secrets.json').read_text(),config)
            self.assertEqual((backup/'bridge.py').read_text(),'old source')
            self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0],'existing-thread')
            self.assertTrue((backup/'state.sqlite3').exists())
            self.assertTrue((root/'gateway.py').exists())
            self.assertEqual((root/'bridge.py').stat().st_mode & 0o777,0o600)
            connection=dict(url='https://gateway.example.com',secret='s'*32,machine_id='m_test',agent_id='codex-bridge')
            installer.install(root,connection)
            merged=json.loads((root/'secrets.json').read_text())
            self.assertEqual(merged['telegram_token'],original['telegram_token'])
            self.assertEqual(merged['codex_environment'],original['codex_environment'])
            self.assertEqual(merged['message_source'],'gateway')
            db.close()

    def test_invalid_connection_is_rejected_before_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'secrets.json').write_text('{}')
            (root/'bridge.py').write_text('old')
            with self.assertRaises(ValueError):
                installer.install(root,dict(url='http://8.8.8.8',secret='s'*32,machine_id='m_test'))
            self.assertEqual((root/'bridge.py').read_text(),'old')


if __name__=='__main__': unittest.main()
