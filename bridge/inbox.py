"""Durable inbox adapter; the existing Bridge still owns Codex, files and decisions."""
import json
import os
import sqlite3
from contextlib import contextmanager


class GatewayInbox:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS inbox(update_id INTEGER PRIMARY KEY,raw_json TEXT NOT NULL,context_json TEXT NOT NULL,priority INTEGER NOT NULL,done INTEGER NOT NULL DEFAULT 0)')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path,timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ingest(self, updates, context):
        with self.connection() as conn:
            for update in updates:
                text = update.get('message',{}).get('text','')
                priority = int('callback_query' in update or text.startswith('/'))
                conn.execute('INSERT OR IGNORE INTO inbox(update_id,raw_json,context_json,priority) VALUES(?,?,?,?)',
                    (update['update_id'],json.dumps(update,ensure_ascii=False),json.dumps(context),priority))

    def next_update(self, controls_only=False):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM inbox WHERE done=0' + (' AND priority=1' if controls_only else '') + ' ORDER BY priority DESC,update_id LIMIT 1').fetchone()
            return (json.loads(row['raw_json']),json.loads(row['context_json'])) if row else None

    def complete(self, update_id):
        with self.connection() as conn:
            conn.execute('UPDATE inbox SET done=1 WHERE update_id=?',(update_id,))


def interaction_is_current(update, saved, current, token_from_callback=None, allowed_users=()):
    """Fail closed for changed prompts/tasks. Interpret original Telegram date, not arrival time.

    Context is supplied by the real Bridge: task_id/task_started_at, prompt_token,
    prompt_message_id/prompt_created_at. Callback token extraction must use its actual format.
    """
    item = update.get('callback_query') or update.get('message',{})
    if not allowed_users or item.get('from',{}).get('id') not in allowed_users:
        return False
    if 'callback_query' in update:
        return bool(token_from_callback and current.get('prompt_token') and
                    saved.get('prompt_token') == current.get('prompt_token') and
                    item.get('message',{}).get('message_id') == current.get('prompt_message_id') and
                    token_from_callback(item.get('data','')) == current['prompt_token'])
    message = update.get('message',{})
    command = (message.get('text') or '').split(' ',1)[0].split('@')[0].lower()
    if command in ('/cancel','/steer'):
        return bool(current.get('task_id') and saved.get('task_id') == current['task_id'] and
                    message.get('date',0) >= current.get('task_started_at',float('inf')))
    if current.get('prompt_token'):
        if saved.get('prompt_token') != current['prompt_token']:
            return False
        if message.get('date',0) < current.get('prompt_created_at',float('inf')):
            return False
        reply_id = message.get('reply_to_message',{}).get('message_id')
        return reply_id is None or reply_id == current.get('prompt_message_id')
    # Text replying to an old prompt is stale even when no prompt is currently pending.
    if saved.get('prompt_token') or (message.get('reply_to_message') and saved.get('prompt_message_id')):
        return False
    return True


class GatewayReceiver:
    """One poll step and one dispatch step for embedding into an existing Bridge.

    handler must durably enqueue into the existing Bridge's own idempotent inbox;
    it must return before starting long Codex work, preserving the control lane.
    """
    def __init__(self, client, inbox, allowed_users, token_from_callback=None):
        self.client, self.inbox = client, inbox
        self.allowed_users, self.token_from_callback = allowed_users, token_from_callback

    def receive(self, context, codex_status='ready', current_task=None, capacity=10):
        self.client.heartbeat(codex_status=codex_status,current_task=current_task,inbox_capacity=capacity)
        return self.client.receive_into(self.inbox,context,limit=capacity)

    def dispatch_one(self, context, handler, stale_notice, controls_only=False):
        pending = self.inbox.next_update(controls_only)
        if not pending:
            return False
        update, saved = pending
        if interaction_is_current(update,saved,context,self.token_from_callback,self.allowed_users):
            handler(update)  # existing Bridge must validate IDs/tokens again and persist before returning
        else:
            stale_notice(update)
        self.inbox.complete(update['update_id'])
        return True
