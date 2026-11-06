"""
VPS Controller SQLite 持久化存储模块
负责持久化：
- tg_updates: Telegram 接收到的消息队列与确认状态 (pending -> delivered -> acked)
- controller_state: 保活模式、租约期限、平台冷却截止时间、机器运行状态
- agent_heartbeats: 虚拟机 Bridge 状态与 Codex 会话就绪情况
- kv_meta: 游标 (tg_offset) 与动态设置
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_db()

    @contextmanager
    def _get_conn(self):
        existing = getattr(self._local, 'conn', None)
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        self._local.conn = conn
        try:
            with conn:
                yield conn
        finally:
            self._local.conn = None
            conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            with self._get_conn() as conn:
                yield conn

    def _init_db(self) -> None:
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with self._lock:
            with self._get_conn() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS tg_updates (
                    update_id INTEGER PRIMARY KEY,
                    chat_id INTEGER,
                    from_user_id INTEGER,
                    message_id INTEGER,
                    text TEXT,
                    raw_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', -- pending, delivered, acked
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tg_updates_status ON tg_updates(status);

                CREATE TABLE IF NOT EXISTS controller_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    machine_id TEXT DEFAULT '',
                    machine_name TEXT DEFAULT '',
                    vm_state TEXT DEFAULT 'unknown',
                    lease_name TEXT DEFAULT 'tg-controller',
                    keep_mode TEXT DEFAULT NULL,
                    hold_until TEXT DEFAULT NULL,
                    lease_expires_at TEXT DEFAULT NULL,
                    last_renew_at TEXT DEFAULT NULL,
                    cooldown_until TEXT DEFAULT NULL,
                    last_error TEXT DEFAULT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_heartbeats (
                    agent_id TEXT PRIMARY KEY,
                    last_seen_at TEXT NOT NULL,
                    version TEXT,
                    codex_status TEXT,
                    current_task TEXT,
                    inbox_capacity INTEGER DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS kv_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)
                state_columns = {'session_started_at': 'TEXT', 'pending_hold_seconds': 'INTEGER DEFAULT 0', 'pending_hold_until': 'TEXT', 'release_requested': 'INTEGER DEFAULT 0', 'resume_key': 'TEXT', 'resume_phase': "TEXT DEFAULT 'idle'", 'resume_operation': 'TEXT', 'resume_reason': 'TEXT', 'resume_explicit': 'INTEGER DEFAULT 0', 'resume_until': 'TEXT', 'resume_retry_at': 'TEXT', 'resume_attempts': 'INTEGER DEFAULT 0', 'api_blocked': 'INTEGER DEFAULT 0', 'auto_suspended_at': 'TEXT', 'suspend_reason': 'TEXT', 'schedule_json': 'TEXT', 'task_id': 'TEXT', 'task_started_at': 'TEXT', 'task_hold_until': 'TEXT', 'agent_valid_after': 'TEXT', 'schedule_active': 'INTEGER DEFAULT 0'}
                existing = {r['name'] for r in conn.execute('PRAGMA table_info(controller_state)')}
                state_columns['api_retry_at'] = 'TEXT'
                state_columns.update(sleep_key='TEXT', sleep_phase="TEXT DEFAULT 'idle'",
                                     sleep_operation='TEXT', sleep_retry_at='TEXT', sleep_until='TEXT')
                for name, declaration in state_columns.items():
                    if name not in existing:
                        conn.execute(f'ALTER TABLE controller_state ADD COLUMN {name} {declaration}')
                existing = {r['name'] for r in conn.execute('PRAGMA table_info(tg_updates)')}
                for name, declaration in {'priority':'INTEGER DEFAULT 0','handled':'INTEGER DEFAULT 0','callback_answered':'INTEGER DEFAULT 0'}.items():
                    if name not in existing:
                        conn.execute(f'ALTER TABLE tg_updates ADD COLUMN {name} {declaration}')
                conn.execute("UPDATE tg_updates SET handled=1 WHERE status IN ('pending','delivered','acked')")
                conn.execute("CREATE TABLE IF NOT EXISTS tg_outbox(id TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,text TEXT NOT NULL,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,sent INTEGER DEFAULT 0)")
                # 初始化单行 controller_state
                cur = conn.execute("SELECT id FROM controller_state WHERE id = 1")
                if not cur.fetchone():
                    conn.execute(
                        "INSERT INTO controller_state (id, updated_at) VALUES (1, ?)",
                        (now_iso(),)
                    )

    # ---- KV Meta (如 Telegram offset) ----
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO kv_meta (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, now_iso())
                )

    # ---- Controller State ----
    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT * FROM controller_state WHERE id = 1").fetchone()
                return dict(row) if row else {}

    def update_state(self, **kwargs) -> None:
        if not kwargs:
            return
        kwargs["updated_at"] = now_iso()
        fields = [f"{k} = ?" for k in kwargs.keys()]
        values = list(kwargs.values())
        sql = f"UPDATE controller_state SET {', '.join(fields)} WHERE id = 1"
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(sql, values)

    # ---- Telegram Updates Queue ----
    def enqueue_update(self, update_id: int, chat_id: Optional[int], from_user_id: Optional[int],
                       message_id: Optional[int], text: Optional[str], raw_json: str, status='pending', priority=0) -> bool:
        """保存 Telegram 消息更新，若 update_id 已存在则忽略（保证幂等）。返回是否为新增更新。"""
        with self._lock:
            with self._get_conn() as conn:
                try:
                    conn.execute(
                        "INSERT INTO tg_updates (update_id, chat_id, from_user_id, message_id, text, raw_json, status, priority, handled, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (update_id, chat_id, from_user_id, message_id, text, raw_json, status, priority, int(status != 'received'), now_iso())
                    )
                    return True
                except sqlite3.IntegrityError:
                    return False

    def pull_pending_updates(self, limit=20, control_limit=20):
        # No visibility timeout: all unacknowledged rows may be redelivered.
        if not 0 <= limit <= 100 or not 0 <= control_limit <= 100:
            raise ValueError('Pull limits must be 0..100')
        with self.transaction() as conn:
            rows = []
            for priority, capacity in [(1,control_limit),(0,limit)]:
                rows.extend(conn.execute("SELECT update_id,raw_json FROM tg_updates WHERE status IN ('pending','delivered') AND priority=? ORDER BY update_id LIMIT ?", (priority,capacity)).fetchall())
            for row in rows:
                conn.execute("UPDATE tg_updates SET status='delivered',delivered_at=? WHERE update_id=?", (now_iso(),row['update_id']))
            return [json.loads(row['raw_json']) for row in rows]

    def promote_prompt_replies(self, prompt):
        """Keep text answers to an active form flowing when the task queue is full."""
        if not prompt:
            return
        if (not isinstance(prompt, dict) or type(prompt.get('message_id')) is not int
                or type(prompt.get('created_at')) not in (int, float) or not 0 < prompt['created_at'] < 10**11):
            raise ValueError('Invalid input prompt')
        with self.transaction() as conn:
            ids = []
            for row in conn.execute("SELECT update_id,raw_json FROM tg_updates WHERE priority=0 AND status IN ('pending','delivered')"):
                message = json.loads(row['raw_json']).get('message', {})
                if (message.get('text') and message.get('date', 0) >= int(prompt['created_at'])
                        and message.get('reply_to_message', {}).get('message_id') in (None, prompt['message_id'])):
                    ids.append((row['update_id'],))
            conn.executemany('UPDATE tg_updates SET priority=1 WHERE update_id=?', ids)

    def ack_updates(self, update_ids: List[int]) -> int:
        """Bridge 确认已入库/已处理的消息"""
        if not update_ids:
            return 0
        if len(update_ids) > 200 or any(type(uid) is not int for uid in update_ids):
            raise ValueError('Invalid update_ids')
        with self._lock:
            with self._get_conn() as conn:
                placeholders = ",".join("?" for _ in update_ids)
                cur = conn.execute(
                    f"UPDATE tg_updates SET status = 'acked', acked_at = ? "
                    f"WHERE status = 'delivered' AND update_id IN ({placeholders})",
                    [now_iso()] + update_ids
                )
                return cur.rowcount

    def get_queue_stats(self) -> Dict[str, int]:
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("""
                SELECT
                    SUM(CASE WHEN status = 'received' THEN 1 ELSE 0 END) AS received,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
                    SUM(CASE WHEN status = 'acked' THEN 1 ELSE 0 END) AS acked,
                    COUNT(*) AS total
                FROM tg_updates
                """).fetchone()
                return {
                    "received": row["received"] or 0,
                    "pending": row["pending"] or 0,
                    "delivered": row["delivered"] or 0,
                    "acked": row["acked"] or 0,
                    "total": row["total"] or 0
                }

    # ---- Agent Heartbeats ----
    def record_heartbeat(self, agent_id: str, version: str, codex_status: str,
                         current_task: Optional[str] = None, inbox_capacity: int = 10) -> None:
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO agent_heartbeats (agent_id, last_seen_at, version, codex_status, current_task, inbox_capacity) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent_id) DO UPDATE SET "
                    "last_seen_at = excluded.last_seen_at, version = excluded.version, "
                    "codex_status = excluded.codex_status, current_task = excluded.current_task, "
                    "inbox_capacity = excluded.inbox_capacity",
                    (agent_id, now_iso(), version, codex_status, current_task, inbox_capacity)
                )

    def get_latest_agent_status(self, agent_id=None) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute(
                    'SELECT * FROM agent_heartbeats WHERE agent_id=?' if agent_id else 'SELECT * FROM agent_heartbeats ORDER BY last_seen_at DESC LIMIT 1',
                    (agent_id,) if agent_id else ()
                ).fetchone()
                return dict(row) if row else None

    def ingest_batch(self, updates):
        # The complete batch and offset are committed before Telegram is acknowledged.
        with self.transaction():
            offset = int(self.get_meta('tg_offset', '0'))
            for update in updates:
                uid = update['update_id']
                if type(uid) is not int:
                    raise ValueError('Invalid Telegram update_id')
                item = update.get('message') or update.get('callback_query', {})
                msg = item.get('message', item)
                text = item.get('text') or item.get('caption') or ''
                priority = int('callback_query' in update or text.startswith('/'))
                self.enqueue_update(uid, msg.get('chat', {}).get('id'), item.get('from', {}).get('id'), msg.get('message_id'), text, json.dumps(update, ensure_ascii=False), 'received', priority)
                offset = max(offset, uid + 1)
            self.set_meta('tg_offset', str(offset))

    def next_received(self):
        with self.transaction() as conn:
            row = conn.execute("SELECT raw_json FROM tg_updates WHERE handled=0 AND status='received' ORDER BY priority DESC,update_id LIMIT 1").fetchone()
            return json.loads(row['raw_json']) if row else None

    def finish_update(self, uid, bridge=False):
        with self.transaction() as conn:
            conn.execute('UPDATE tg_updates SET handled=1,status=? WHERE update_id=?', ('pending' if bridge else 'acked', uid))

    def is_handled(self, uid):
        with self.transaction() as conn:
            row = conn.execute('SELECT handled FROM tg_updates WHERE update_id=?', (uid,)).fetchone()
            return bool(row and row['handled'])

    def add_reply(self, key, chat_id, text):
        if chat_id is not None:
            with self.transaction() as conn:
                conn.execute('INSERT OR IGNORE INTO tg_outbox(id,chat_id,text) VALUES(?,?,?)', (str(key),chat_id,text))

    def next_reply(self, now):
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM tg_outbox WHERE sent=0 AND retry_at<=? ORDER BY rowid LIMIT 1', (now,)).fetchone()
            return dict(row) if row else None

    def reply_result(self, key, sent, retry_at=0):
        with self.transaction() as conn:
            conn.execute('UPDATE tg_outbox SET sent=?,attempts=attempts+1,retry_at=? WHERE id=?', (int(sent),retry_at,key))

    def next_callback(self):
        with self.transaction() as conn:
            row = conn.execute("SELECT update_id,raw_json FROM tg_updates WHERE callback_answered=0 AND instr(raw_json, 'callback_query')>0 ORDER BY update_id LIMIT 1").fetchone()
            return (row['update_id'],json.loads(row['raw_json'])) if row else None

    def mark_callback_answered(self, uid):
        with self.transaction() as conn:
            conn.execute('UPDATE tg_updates SET callback_answered=1 WHERE update_id=?',(uid,))
