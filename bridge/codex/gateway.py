"""Self-contained gateway transport for the existing bridge.py runtime."""
import ipaddress
import json
import os
import sqlite3
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlsplit


class GatewayError(Exception):
    pass


class GatewayClient:
    def __init__(self, url, secret, machine_id, agent_id='codex-bridge'):
        parsed = urlsplit(url)
        private = parsed.hostname == 'localhost'
        try:
            address = ipaddress.ip_address(parsed.hostname or '')
            private = address.is_private or address.is_loopback
        except ValueError:
            pass
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.scheme not in ('https', 'http')
                or (parsed.scheme == 'http' and not private)):
            raise ValueError('Gateway requires HTTPS or a private IP')
        if len(secret) < 32 or not machine_id.startswith('m_') or not agent_id:
            raise ValueError('Gateway requires a shared secret and fixed machine/agent identity')
        self.url, self.secret = url.rstrip('/'), secret
        self.machine_id, self.agent_id = machine_id, agent_id

    def post(self, path, **payload):
        body = dict(payload, machine_id=self.machine_id, agent_id=self.agent_id)
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'X-Agent-Secret': self.secret}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            if not isinstance(result, dict) or not result.get('ok'):
                raise GatewayError('Gateway rejected request')
            return result
        except (OSError, ValueError):
            # Never propagate urllib exception strings containing connection credentials.
            raise GatewayError('Gateway request unavailable') from None


class GatewayInbox:
    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS inbox(update_id INTEGER PRIMARY KEY, raw TEXT NOT NULL, context TEXT NOT NULL, priority INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0)')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, updates, context):
        with self.connection() as db:
            for update in updates:
                uid = update['update_id']
                if type(uid) is not int:
                    raise ValueError('Invalid update ID')
                priority = int('callback_query' in update or update.get('message', {}).get('text', '').startswith('/'))
                message = update.get('message', {})
                if (context.get('prompt_token') and message.get('text')
                        and message.get('date', 0) >= int(context.get('prompt_created_at') or 0)
                        and message.get('reply_to_message', {}).get('message_id') in (None, context.get('prompt_message_id'))):
                    priority = 1
                db.execute('INSERT OR IGNORE INTO inbox(update_id,raw,context,priority) VALUES(?,?,?,?)',
                    (uid, json.dumps(update, ensure_ascii=False), json.dumps(context), priority))

    def pending(self, controls_only=False):
        with self.connection() as db:
            row = db.execute('SELECT * FROM inbox WHERE done=0' + (' AND priority=1' if controls_only else '')
                + ' ORDER BY priority DESC,update_id LIMIT 1').fetchone()
            return (json.loads(row['raw']), json.loads(row['context'])) if row else None

    def count_ordinary(self):
        with self.connection() as db:
            return db.execute('SELECT COUNT(*) FROM inbox WHERE done=0 AND priority=0').fetchone()[0]

    def complete(self, uid):
        with self.connection() as db:
            db.execute('UPDATE inbox SET done=1 WHERE update_id=?', (uid,))
