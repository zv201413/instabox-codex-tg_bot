#!/usr/bin/env python3
"""Telegram client for the official Codex app-server. Python standard library only."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from elicitation import Action, Elicitation
from gateway import GatewayClient, GatewayInbox, GatewayError

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOTS = ('/data/workspace', '/data/home')
LOG = logging.getLogger("telegram-codex")
VERSION = "1.2.0"
HELP = """直接发文字、图片或文件即可给 Codex 下任务；对话上下文会保留。

/new — 新建对话（保留旧记录）
/cancel — 取消当前任务
/clearqueue — 清空等待中的任务
/steer 内容 — 给正在执行的任务追加说明
/status — 查看模型、任务和虚拟机运行时间
/retry — 重试最近失败或中断的任务
/file 路径 — 下载工作目录中的文件
/help — 显示帮助

需要审批时点击按钮；需要补充信息时直接回复。
工具表单可逐项填写、点选或多选；填写后点击确认提交，也可拒绝或取消。
本机连续运行上限约 8 小时，空闲 30 分钟可能休眠。休眠时无法回复，请在 instabox 面板唤醒；运行时限触发的休眠可能有冷却期。唤醒后会重连并保留对话。"""


def chunks(text: str, limit: int = 3500) -> list[str]:
    """Telegram limits count UTF-16 code units, including two units for emoji."""
    result, current, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            part = "".join(current)
            # Prefer paragraph boundaries without making tiny chunks.
            cut = part.rfind("\n", len(part) // 2)
            if cut > 0:
                result.append(part[:cut])
                current = list(part[cut + 1:])
                size = len("".join(current).encode("utf-16-le")) // 2
            else:
                result.append(part)
                current, size = [], 0
        current.append(char)
        size += width
    if current:
        result.append("".join(current))
    return result or ["（暂无文本输出）"]


def render(text: str) -> str:
    """Convert common Markdown to a small, escaped Telegram HTML subset."""
    blocks = []

    def code(match):
        blocks.append("<pre>" + html.escape(match.group(1)) + "</pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```[^\n]*\n(.*?)```", code, text, flags=re.S)
    text = html.escape(text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^#{1,6} (.+)$", r"<b>\1</b>", text)
    # Links are deliberately left visible as Markdown text; no untrusted HTML.
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram {code}: {description}")


class JobCancelled(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.write_lock = asyncio.Lock()
        self.last_write = 0.0

    def _call(self, method, params, timeout, multipart=None):
        if multipart:
            path, caption = multipart
            boundary = secrets.token_hex(20)
            fields = {"chat_id": str(params["chat_id"]), "caption": caption[:900]}
            data = bytearray()
            for key, value in fields.items():
                data.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            filename = re.sub(r"[^\w. -]", "_", path.name)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            data.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            data.extend(path.read_bytes())
            data.extend(f"\r\n--{boundary}--\r\n".encode())
            headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
        else:
            data = json.dumps(params).encode()
            headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(self.base + method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.load(error)
            except Exception:
                raise TelegramError(error.code, "HTTP request failed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # urllib exceptions include the URL, which contains the token.
            raise TelegramError(0, "网络连接暂时不可用") from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", 0), body.get("description", "request failed"), body.get("parameters", {}).get("retry_after", 0))
        return body["result"]

    async def call(self, method, **params):
        write = method in {"sendMessage", "editMessageText", "sendDocument"}
        multipart = params.pop("_multipart", None)

        async def perform():
            for attempt in range(4):
                if write:
                    await asyncio.sleep(max(0, 1.05 - (time.monotonic() - self.last_write)))
                    self.last_write = time.monotonic()
                try:
                    return await asyncio.to_thread(self._call, method, params, 60 if multipart else 40, multipart)
                except TelegramError as error:
                    if error.code == 429 and attempt < 3:
                        await asyncio.sleep(max(1, error.retry_after))
                    elif error.code >= 500 and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        raise
        if write:
            async with self.write_lock:
                return await perform()
        return await perform()

    async def send(self, chat, text, reply=None, keyboard=None):
        args = {"chat_id": chat, "text": render(text), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if reply:
            args["reply_parameters"] = {"message_id": reply, "allow_sending_without_reply": True}
        if keyboard:
            args["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            return await self.call("sendMessage", **args)
        except TelegramError as error:
            if error.code == 400 and "parse" in str(error).lower():
                args.pop("parse_mode")
                args["text"] = text
                return await self.call("sendMessage", **args)
            raise

    async def edit(self, chat, message, text, keyboard=None):
        args = {"chat_id": chat, "message_id": message, "text": render(text), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}, "reply_markup": {"inline_keyboard": keyboard or []}}
        try:
            return await self.call("editMessageText", **args)
        except TelegramError as error:
            if "message is not modified" in str(error).lower():
                return
            if error.code == 400 and "parse" in str(error).lower():
                args.pop("parse_mode")
                args["text"] = text
                return await self.call("editMessageText", **args)
            raise

    async def download(self, file_id, destination):
        data = await self.call("getFile", file_id=file_id)
        if data.get("file_size", 0) > 20 * 1024 * 1024:
            raise ValueError("附件超过 20 MB，请发送较小的文件。")
        url = "https://api.telegram.org/file/bot" + self.token + "/" + data["file_path"]

        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    payload = response.read(20 * 1024 * 1024 + 1)
                if len(payload) > 20 * 1024 * 1024:
                    raise ValueError("附件超过 20 MB。")
                destination.write_bytes(payload)
                destination.chmod(0o600)
            except (urllib.error.URLError, TimeoutError, OSError):
                raise TelegramError(0, "附件下载失败，请重新发送。") from None
        await asyncio.to_thread(fetch)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, message TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, thread_id TEXT, turn_id TEXT, result TEXT,
                live_message INTEGER, delivered INTEGER NOT NULL DEFAULT 0
            );
        """)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def add(self, update_id, message):
        cursor = self.db.execute("INSERT OR IGNORE INTO jobs(id,message,status,created) VALUES (?,?,'queued',?)", (update_id, json.dumps(message), time.time()))
        self.db.commit()
        return bool(cursor.rowcount)

    def update(self, job_id, **fields):
        assert set(fields) <= {"status", "thread_id", "turn_id", "result", "live_message", "delivered"}
        self.db.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", [*fields.values(), job_id])
        self.db.commit()

    def queued(self):
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]


class RPC:
    def __init__(self, environment):
        self.environment = environment
        self.pending = {}
        self.events = asyncio.Queue()
        self.next_id = 0
        self.proc = None
        self.reader = None
        self.stderr = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "/usr/local/bin/codex", "app-server", "--stdio",
            "-c", 'features.multi_agent=false',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.environment, limit=16 * 1024 * 1024,
            cwd="/data/workspace")
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._stderr())
        result = await self.request("initialize", {
            "clientInfo": {"name": "telegram_codex_local", "title": "Telegram Codex", "version": VERSION},
            "capabilities": {"experimentalApi": True, "optOutNotificationMethods": ["item/reasoning/textDelta", "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta"]}
        })
        await self.send({"method": "initialized", "params": {}})
        LOG.info("Codex app-server ready: %s", result.get("platformOs", "linux"))

    async def _read(self):
        try:
            async for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending.pop(message["id"])
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"].get("message", "Codex RPC error")))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    await self.events.put(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex app-server 连接中断"))
            self.pending.clear()
            await self.events.put({"method": "bridge/serverExited", "params": {}})

    async def _stderr(self):
        # Never log arbitrary upstream URLs, command arguments, credentials or user text.
        async for _ in self.proc.stderr:
            pass

    async def send(self, message):
        if self.proc.returncode is not None:
            raise ConnectionError("Codex app-server 已退出")
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params, timeout=60):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 10)
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        for task in [self.reader, self.stderr]:
            if task:
                task.cancel()
        await asyncio.gather(*[task for task in [self.reader, self.stderr] if task], return_exceptions=True)


class Bridge:
    def __init__(self, config):
        self.config = config
        self.message_source = config.get("message_source", "telegram")
        if self.message_source not in {"telegram", "gateway"}:
            raise ValueError("message_source must be telegram or gateway")
        self.gateway = None
        self.gateway_inbox = None
        self.gateway_changed = asyncio.Event()
        self.rpc_ready = False
        if self.message_source == "gateway":
            values = config.get("gateway", {})
            self.gateway = GatewayClient(values.get("url", ""), values.get("secret", ""),
                                         values.get("machine_id", ""), values.get("agent_id", "codex-bridge"))
            self.gateway_inbox = GatewayInbox(ROOT / "gateway-inbox.sqlite3")
        self.tg = Telegram(config["telegram_token"])
        self.store = Store(ROOT / "state.sqlite3")
        environment = dict(os.environ)
        environment.update(config.get("codex_environment", {}))
        for name in list(environment):
            if name.startswith("CODEX_") or name in {"TELEGRAM_BOT_TOKEN", "INSTABOX_API_KEY"}:
                environment.pop(name)
        self.rpc = RPC(environment)
        self.active = None
        self.loaded_thread = None
        self.pending_inputs = {}
        self.prompts = {}
        self.prompt_tasks = set()
        self.server_requests = {}
        self.interaction_lock = asyncio.Lock()
        self.current_question = None
        self.wake = asyncio.Event()
        self.session_lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.credentials = [config["telegram_token"], config.get("gateway", {}).get("secret", ""), *config.get("codex_environment", {}).values()]
        self.started = time.time()
        self.run_epoch = self.started - float(Path("/proc/uptime").read_text().split()[0])
        self.last_input = self.store.get("last_input", time.time())
        self.idle_notice = False
        self.cap_notice = False
        self.unpaired_notices = {}

    def safe(self, text):
        text = str(text)
        for credential in self.credentials:
            if credential:
                text = text.replace(credential, "[凭据已隐藏]")
        return re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b", "[Bot Token 已隐藏]", text)

    @property
    def owner(self):
        return self.store.get("owner")

    def authorized(self, message):
        owner = self.owner
        return bool(owner and message.get("chat", {}).get("type") == "private" and
                    message.get("from", {}).get("id") == owner["user_id"] and
                    message.get("chat", {}).get("id") == owner["chat_id"] and
                    not message.get("from", {}).get("is_bot"))

    async def say(self, text, reply=None, keyboard=None):
        if self.owner:
            return await self.tg.send(self.owner["chat_id"], self.safe(text), reply, keyboard)

    async def run(self):
        info = await self.tg.call("getMe")
        webhook = await self.tg.call("getWebhookInfo")
        if webhook.get("url"):
            raise RuntimeError("Bot 已配置 webhook；未覆盖现有连接。")
        LOG.info("Telegram connected: @%s", info["username"])
        await self.rpc.start()
        self.rpc_ready = True
        await self.tg.call("setMyCommands", commands=[{"command": x, "description": y} for x, y in [
            ("new", "新建对话"), ("cancel", "取消当前任务"), ("status", "任务和虚拟机状态"),
            ("help", "使用帮助"), ("steer", "给当前任务追加说明"), ("clearqueue", "清空任务队列"),
            ("retry", "重试最近失败或中断的任务"), ("file", "下载工作目录中的文件"),
            *([( "resume", "唤醒虚拟机"), ("hold", "自定义保活，最长8小时"), ("release", "结束保活"),
               ("sleep", "休眠并保留任务"), ("vmstatus", "机器与保活状态"), ("mode", "保活模式"), ("schedule", "保活计划")]
              if self.gateway else [])]])
        await self.tg.call("setMyDescription", description=("通过此机器人使用 Codex；/resume 唤醒、/hold 自定义保活、/sleep 休眠。仅绑定账号可用。" if self.gateway else "通过此机器人使用本机 Codex。虚拟机休眠时暂时离线；唤醒后恢复连接和对话。仅绑定账号可用。"))
        interrupted = self.store.db.execute("SELECT id,thread_id,turn_id FROM jobs WHERE status='running'").fetchall()
        for row in interrupted:
            self.store.update(row["id"], status="interrupted", result="服务重启时检测到未完成任务；没有自动重复执行。")
        if self.owner:
            updated = self.store.get("bridge_version") != VERSION
            await self.say("连接已恢复，对话记录已保留。" + ("\n工具表单支持已生效，可直接在 TG 填写和提交。" if updated else "") + ("\n上次任务未完成；请查看 /status，或用 /retry 重试。" if interrupted else ""))
            self.store.set("bridge_version", VERSION)
            for row in self.store.db.execute("SELECT id,result,live_message FROM jobs WHERE result IS NOT NULL AND delivered=0 ORDER BY id").fetchall():
                await self.deliver_result(row["id"], row["result"], row["live_message"])
        transport = [self.gateway_poll(), self.gateway_heartbeat(), self.gateway_dispatch()] if self.gateway else [self.poll()]
        tasks = [asyncio.create_task(coro) for coro in transport]
        tasks += [asyncio.create_task(self.worker()), asyncio.create_task(self.events()), asyncio.create_task(self.lifecycle())]
        stop_task = asyncio.create_task(self.stopping.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stop_task:
                    await task
        finally:
            for task in [*tasks, stop_task, *self.prompt_tasks]:
                task.cancel()
            await asyncio.gather(*tasks, stop_task, *self.prompt_tasks, return_exceptions=True)
            self.rpc_ready = False
            await self.rpc.close()

    def help_text(self):
        if not self.gateway:
            return HELP
        return HELP.split("本机连续运行上限")[0] + """/resume — 唤醒机器
/hold 30m、/hold 2h、/hold 8h — 自定义保活，最长 8 小时
/release — 结束保活，按平台空闲规则休眠
/sleep — 立即休眠，保留进程和对话
/vmstatus — 机器、保活和冷却状态
/mode auto|manual — 自动按需或手动保活
/schedule — 保活计划

VPS 在机器休眠时继续接收本 bot 的消息。/sleep 后需要明确 /resume 或 /hold 唤醒。
保活不延长平台运行上限；如有冷却，恢复请求等待截止后重试。"""

    def gateway_context(self):
        prompt = next(((token, value) for token, value in self.prompts.items()
                       if value["future"] is self.current_question and not value["future"].done()), None)
        return {"task_id": str(self.active["id"]) if self.active and "id" in self.active else None,
                "task_started_at": self.active.get("started") if self.active else None,
                "prompt_token": prompt[0] if prompt else None,
                "prompt_message_id": prompt[1].get("message_id") if prompt else None,
                "prompt_created_at": prompt[1].get("created_at") if prompt else None}

    def gateway_capacity(self):
        return max(0, 20 - self.store.queued() - self.gateway_inbox.count_ordinary())

    async def gateway_heartbeat_once(self):
        status = "busy" if self.active else "ready" if self.rpc_ready else "starting"
        await asyncio.to_thread(self.gateway.post, "/agent/heartbeat", version=VERSION,
                                codex_status=status, current_task=self.gateway_context()["task_id"],
                                inbox_capacity=self.gateway_capacity())

    async def gateway_heartbeat(self):
        while True:
            try:
                await self.gateway_heartbeat_once()
            except GatewayError:
                LOG.warning("Gateway heartbeat unavailable")
            await asyncio.sleep(20)

    async def gateway_receive_once(self):
        context = self.gateway_context()
        result = await asyncio.to_thread(self.gateway.post, "/agent/pull",
                                        limit=self.gateway_capacity(), control_limit=20,
                                        input_prompt={"message_id": context["prompt_message_id"],
                                                      "created_at": context["prompt_created_at"]} if context["prompt_token"] else None)
        updates = result.get("updates", [])
        self.gateway_inbox.ingest(updates, context)
        # Disk errors propagate and stop intake; never ack messages not committed locally.
        if updates:
            self.gateway_changed.set()
            await asyncio.to_thread(self.gateway.post, "/agent/ack", update_ids=[u["update_id"] for u in updates])
        return len(updates)

    async def gateway_poll(self):
        while True:
            try:
                await self.gateway_receive_once()
            except GatewayError:
                LOG.warning("Gateway intake unavailable; retaining local inbox")
            await asyncio.sleep(2)

    def gateway_interaction_current(self, update, saved):
        message = update.get("message", {})
        if "callback_query" in update:
            query = update["callback_query"]
            if not self.authorized({**query.get("message", {}), "from": query.get("from", {})}):
                return False
            match = re.fullmatch(r"prompt:([a-f0-9]+):(\d+)", query.get("data", ""))
            prompt = self.prompts.get(match[1]) if match else None
            return bool(prompt and not prompt["future"].done()
                        and int(match[2]) < len(prompt["choices"])
                        and query.get("message", {}).get("message_id") == prompt.get("message_id"))
        if self.owner and not self.authorized(message):
            return False
        first_word = (message.get("text", "").split(maxsplit=1) or [""])[0]
        command = first_word.split("@")[0] if re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]*(?:@\w+)?", first_word) else ""
        current = self.gateway_context()
        if command in {"/cancel", "/steer"}:
            return bool(current["task_id"] and saved.get("task_id") == current["task_id"]
                        and current["task_started_at"] is not None
                        and message.get("date", 0) >= int(current["task_started_at"]))
        if command.startswith("/"):
            return True
        reply_id = message.get("reply_to_message", {}).get("message_id")
        history = self.store.get("gateway_prompt_messages", [])
        if reply_id in history and reply_id != current["prompt_message_id"]:
            return False
        if saved.get("prompt_token") != current["prompt_token"]:
            return False
        if current["prompt_token"]:
            return message.get("date", 0) >= int(current["prompt_created_at"])
        return True

    async def gateway_dispatch_once(self):
        pending = self.gateway_inbox.pending(controls_only=self.store.queued() >= 20)
        if not pending:
            return False
        update, saved = pending
        try:
            if self.gateway_interaction_current(update, saved):
                await self.handle(update)
            elif self.owner:
                item = update.get("callback_query") or update.get("message", {})
                sender = item.get("from", {}).get("id")
                if sender == self.owner["user_id"]:
                    await self.say("此交互对应的问题或任务已结束，请查看最新消息后再操作。")
        except TelegramError:
            # A network error during the reply must not replay a command/selection.
            LOG.warning("Gateway update reply unavailable: %s", update["update_id"])
        self.gateway_inbox.complete(update["update_id"])
        self.wake.set()
        return True

    async def gateway_dispatch(self):
        while True:
            if not await self.gateway_dispatch_once():
                self.gateway_changed.clear()
                try:
                    await asyncio.wait_for(self.gateway_changed.wait(), 1)
                except TimeoutError:
                    pass

    async def poll(self):
        delay = 1
        while True:
            try:
                updates = await self.tg.call("getUpdates", offset=self.store.get("offset", 0), timeout=25, allowed_updates=["message", "callback_query"])
                delay = 1
                for update in updates:
                    try:
                        await self.handle(update)
                    except Exception as error:
                        LOG.warning("Update %s failed: %s", update["update_id"], type(error).__name__)
                        # Authorized task rows are durable; failed command handling is reported.
                        if self.authorized(update.get("message", {})):
                            with contextlib.suppress(TelegramError):
                                await self.say("消息处理失败：" + self.safe(error), update["message"]["message_id"])
                    self.store.set("offset", update["update_id"] + 1)
                self.wake.set()
            except TelegramError as error:
                if error.code in {401, 409}:
                    raise RuntimeError("Bot Token 无效，或已有另一处服务在接收此 Bot 的消息。") from None
                LOG.warning("Telegram polling unavailable; retry in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def handle(self, update):
        if "callback_query" in update:
            await self.callback(update["callback_query"])
            return
        message = update.get("message", {})
        if message.get("chat", {}).get("type") != "private" or message.get("from", {}).get("is_bot"):
            return
        text = message.get("text", "")
        if not self.owner:
            match = re.fullmatch(r"/start(?:@\w+)?\s+([A-Za-z0-9_-]+)", text)
            if match and hmac.compare_digest(match[1], self.config["pairing_code"]):
                self.store.set("owner", {"user_id": message["from"]["id"], "chat_id": message["chat"]["id"]})
                self.last_input = time.time()
                self.store.set("last_input", self.last_input)
                await self.say("绑定成功。现在直接发消息即可使用 Codex。\n\n" + self.help_text())
                LOG.info("Owner paired")
            else:
                sender = message["from"]["id"]
                if time.time() - self.unpaired_notices.get(sender, 0) >= 30:
                    self.unpaired_notices[sender] = time.time()
                    await self.tg.send(message["chat"]["id"], "尚未完成账号绑定。请从配置机器人的 Codex 对话中复制完整命令：\n\n/start 绑定码\n\n单独发送“启动”或 /start 不会绑定。", message["message_id"])
                    if len(self.unpaired_notices) > 1000:
                        self.unpaired_notices.clear()
            return
        if not self.authorized(message):
            return
        self.last_input = time.time()
        self.store.set("last_input", self.last_input)
        self.idle_notice = False
        first_word = text.split(maxsplit=1)[0] if text else ""
        command = first_word.split("@")[0] if re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]*(?:@\w+)?", first_word) else ""
        argument = text.split(maxsplit=1)[1] if " " in text else ""
        if command in {"/start", "/help"}:
            await self.say(self.help_text(), message["message_id"])
        elif command == "/status":
            await self.say(self.status(), message["message_id"])
        elif command == "/cancel":
            if self.active and self.active.get("turn_id"):
                await self.rpc.request("turn/interrupt", {"threadId": self.active["thread_id"], "turnId": self.active["turn_id"]})
                await self.say("已请求取消当前任务。", message["message_id"])
            elif self.active:
                self.active["cancel_requested"] = True
                await self.say("已请求取消，任务准备完成后会立即停止。", message["message_id"])
            else:
                await self.say("当前没有正在执行的任务。", message["message_id"])
        elif command == "/clearqueue":
            count = self.store.queued()
            self.store.db.execute("UPDATE jobs SET status='cancelled' WHERE status='queued'")
            self.store.db.commit()
            await self.say(f"已清空 {count} 个等待中的任务。", message["message_id"])
        elif command == "/new":
            async with self.session_lock:
                if self.active or self.store.queued():
                    await self.say("请先用 /cancel 取消当前任务，并用 /clearqueue 清空等待任务，再新建对话。", message["message_id"])
                else:
                    self.store.set("thread_id", None)
                    self.loaded_thread = None
                    await self.say("已切换到新对话。旧记录仍保留在本机 Codex 中。", message["message_id"])
        elif command == "/steer":
            if not argument:
                await self.say("用法：/steer 追加说明", message["message_id"])
            elif not self.active or not self.active.get("turn_id"):
                await self.say("当前没有可追加说明的任务，请直接发送新消息。", message["message_id"])
            else:
                await self.rpc.request("turn/steer", {"threadId": self.active["thread_id"], "expectedTurnId": self.active["turn_id"], "input": [{"type": "text", "text": argument}], "clientUserMessageId": f"tg-steer-{update['update_id']}"})
                await self.say("追加说明已交给当前任务。", message["message_id"])
        elif command == "/retry":
            row = self.store.db.execute("SELECT message FROM jobs WHERE status IN ('failed','interrupted') ORDER BY id DESC LIMIT 1").fetchone()
            if self.store.queued() >= 20:
                await self.say("等待队列已满，请等当前任务完成。", message["message_id"])
            elif row:
                original = json.loads(row[0])
                original["message_id"] = message["message_id"]
                self.store.add(update["update_id"], original)
                self.wake.set()
                await self.say("已将最近失败或中断的任务加入队列。", message["message_id"])
            else:
                await self.say("没有可重试的任务。", message["message_id"])
        elif command == "/file":
            if not argument:
                await self.say("用法：/file /data/workspace/文件名", message["message_id"])
            else:
                await self.send_file(argument)
        elif command:
            await self.say("未知命令。直接发消息开始任务，或用 /help 查看帮助。", message["message_id"])
        elif self.current_question and text:
            future = self.current_question
            if not future.done():
                reply = message.get("reply_to_message", {}).get("message_id")
                prompt = next((p for p in self.prompts.values() if p["future"] is future), None)
                if reply is not None and prompt and reply != prompt.get("message_id"):
                    await self.say("请回复最新的表单问题，或直接发送答案。", message["message_id"])
                    return
                future.set_result(text)
        elif text or message.get("photo") or message.get("document"):
            if self.store.queued() >= 20:
                await self.say("等待队列已满（20 条），请等当前任务完成。", message["message_id"])
            elif self.store.add(update["update_id"], message):
                if self.active or self.store.queued() > 1:
                    await self.say(f"已收到，当前排队 {self.store.queued()} 条。可用 /steer 追加到当前任务。", message["message_id"])
                self.wake.set()
        else:
            await self.say("目前支持文字、图片和文件。语音请先转成文字再发送。", message["message_id"])

    def status(self):
        remaining = max(0, 8 * 3600 - (time.time() - self.run_epoch))
        model = self.store.get("model", "沿用本机配置")
        lines = [f"连接正常 · 模型：{model}", "工作目录：/data/workspace", f"等待队列：{self.store.queued()} 条"]
        if self.active:
            lines.append(f"当前任务：{self.active.get('progress', '准备中')} · {int(time.time() - self.active['started'])} 秒")
            lines.append("/cancel 取消；/steer 内容 追加说明")
        else:
            lines.append("当前没有正在执行的任务。")
            last = self.store.db.execute("SELECT status,result FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                label = {"done": "已完成", "failed": "失败", "interrupted": "中断", "cancelled": "取消", "queued": "等待中"}.get(last["status"], last["status"])
                lines.append("最近任务：" + label)
                if last["status"] in {"failed", "interrupted"}:
                    lines.append((last["result"] or "")[:300])
        lines.append(f"本轮运行剩余时间估计：{int(remaining)//3600} 小时 {int(remaining)%3600//60} 分钟")
        lines.append("运行上限约 8 小时；本机空闲 30 分钟可能休眠。" + ("休眠后可在此 bot 使用 /resume 唤醒，/vmstatus 查看冷却。" if self.gateway else "休眠后请在 instabox 面板唤醒，运行时限休眠可能需要等待冷却。") + "")
        return "\n".join(lines)

    async def ensure_thread(self):
        thread_id = self.store.get("thread_id")
        if thread_id and self.loaded_thread == thread_id:
            return thread_id
        params = {"cwd": "/data/workspace", "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": "workspace-write"}
        if thread_id:
            response = await self.rpc.request("thread/resume", {**params, "threadId": thread_id, "excludeTurns": True})
        else:
            params["developerInstructions"] = "用户通过 Telegram 私聊与你协作。默认用简洁中文回答，及时说明进度。直接完成已授权任务；需审批或澄清时使用相应工具，客户端会显示 Telegram 按钮或问题。工作文件放在 /data/workspace。可以读取用户发送的附件。要把生成的文件交给用户，请使用 telegram_send_file 工具。不要在回复、命令输出或附件中暴露凭据。虚拟机连续运行上限约8小时，空闲30分钟可能休眠；长任务及时保存进度和文件。"
            params["dynamicTools"] = [{"type": "function", "name": "telegram_send_file", "description": "将工作目录中的生成文件作为附件发回当前绑定的 Telegram 用户。只用于完成用户已请求的任务。", "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "文件绝对路径"}, "caption": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}]
            response = await self.rpc.request("thread/start", params)
            thread_id = response["thread"]["id"]
            self.store.set("thread_id", thread_id)
            with contextlib.suppress(Exception):
                await self.rpc.request("thread/name/set", {"threadId": thread_id, "name": "Telegram · insta_zv_bot"})
        self.loaded_thread = thread_id
        self.store.set("model", response.get("model", "沿用本机配置"))
        return thread_id

    async def input_items(self, message, job_id):
        text = message.get("text") or message.get("caption") or ""
        items = []
        upload = message.get("document") or (message["photo"][-1] if message.get("photo") else None)
        if upload:
            filename = upload.get("file_name", "photo.jpg")
            filename = re.sub(r"[^\w. -]", "_", Path(filename).name)[:120] or "attachment"
            destination = ROOT / "uploads" / f"{job_id}-{filename}"
            await self.tg.download(upload["file_id"], destination)
            if message.get("photo") or upload.get("mime_type", "").startswith("image/"):
                items.append({"type": "localImage", "path": str(destination)})
                text = text or "请查看这张图片。"
            else:
                text = (text or "请查看这个文件。") + "\n\n用户附件路径：" + str(destination)
        return [{"type": "text", "text": text}, *items]

    async def worker(self):
        while True:
            row = self.store.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.wake.clear()
                await self.wake.wait()
                continue
            message = json.loads(row["message"])
            async with self.session_lock:
                self.active = {"id": row["id"], "started": time.time(), "progress": "正在准备", "stream": "", "messages": [], "items": {}, "finished": asyncio.get_running_loop().create_future(), "last_event": time.time()}
                self.store.update(row["id"], status="running")
            progress_task = None
            try:
                live = await self.say("已收到，正在准备…", message["message_id"])
                self.active["live_message"] = live["message_id"]
                self.store.update(row["id"], live_message=live["message_id"])
                progress_task = asyncio.create_task(self.progress())
                thread_id = await self.ensure_thread()
                self.active["thread_id"] = thread_id
                self.store.update(row["id"], thread_id=thread_id)
                inputs = await self.input_items(message, row["id"])
                if self.active.get("cancel_requested"):
                    raise JobCancelled()
                response = await self.rpc.request("turn/start", {"threadId": thread_id, "input": inputs, "clientUserMessageId": f"telegram-{row['id']}"})
                self.active["turn_id"] = response["turn"]["id"]
                self.store.update(row["id"], turn_id=self.active["turn_id"])
                if self.active.get("cancel_requested"):
                    await self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": self.active["turn_id"]})
                turn = await self.active["finished"]
                status = turn["status"]
                finals = [x["text"] for x in self.active["messages"] if x.get("phase") == "final_answer"]
                if not finals:
                    finals = [x["text"] for x in self.active["messages"] if x.get("phase") != "commentary"][-1:]
                answer = "\n\n".join(finals).strip()
                if status == "failed":
                    answer = "任务失败：" + self.safe(turn.get("error", {}).get("message", "未知错误")) + "\n可用 /retry 重试，或发送新的说明。"
                elif status == "interrupted":
                    answer = (answer + "\n\n" if answer else "") + "任务已取消。已保存的文件和对话仍保留。"
                elif not answer:
                    answer = "任务已完成。" + ("\n" + self.active.get("stream", "") if self.active.get("stream") else "")
                self.store.update(row["id"], status="done" if status == "completed" else status, result=self.safe(answer))
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
                await self.deliver_result(row["id"], answer, live["message_id"])
            except JobCancelled:
                self.store.update(row["id"], status="interrupted", result="任务已中断。")
                if self.active.get("live_message"):
                    await self.deliver_result(row["id"], "任务已取消。", self.active["live_message"])
            except asyncio.CancelledError:
                # Cancellation shuts down this worker; never swallow it and restart the loop.
                existing = self.store.db.execute("SELECT status FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if existing["status"] == "running":
                    self.store.update(row["id"], status="interrupted", result="服务停止，任务未完成。请确认执行结果后用 /retry 重试。")
                raise
            except Exception as error:
                LOG.warning("Job %s failed: %s", row["id"], type(error).__name__)
                saved = self.store.db.execute("SELECT status FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if saved["status"] != "running":
                    # Work already completed; a delivery failure must not convert it into a retryable task.
                    raise
                self.store.update(row["id"], status="failed", result=self.safe(error))
                with contextlib.suppress(TelegramError):
                    await self.deliver_result(row["id"], "任务连接失败：" + self.safe(error) + "\n对话仍保留；可用 /retry 重试。", self.active.get("live_message"))
                if isinstance(error, ConnectionError):
                    raise
            finally:
                if progress_task:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                pending = list(self.prompt_tasks)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for prompt in list(self.prompts.values()):
                    prompt["future"].cancel()
                self.prompts.clear()
                self.current_question = None
                self.active = None

    async def deliver_result(self, job_id, answer, live_message=None):
        # Persist results before delivery. An offline Telegram connection must never rerun work.
        delay = 2
        while True:
            try:
                await self.deliver(answer, live_message)
                self.store.update(job_id, delivered=1)
                return
            except TelegramError as error:
                if error.code in {401, 403}:
                    # Retain the completed result for delivery after credentials/access recover.
                    raise ConnectionError("Telegram 无法投递结果，结果已保存") from None
                LOG.warning("Result delivery pending for job %s", job_id)
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def deliver(self, answer, live_message=None):
        parts = chunks(self.safe(answer))
        for i, part in enumerate(parts):
            if i == 0 and live_message:
                try:
                    await self.tg.edit(self.owner["chat_id"], live_message, part)
                    continue
                except TelegramError as error:
                    if error.code != 400:
                        raise
            await self.say(part)

    async def progress(self):
        previous = ""
        while self.active:
            await asyncio.sleep(2)
            active = self.active
            if not active:
                return
            elapsed = int(time.time() - active["started"])
            stream = active.get("stream", "")
            if stream:
                body = chunks(stream, 2700)[-1]
                text = body + f"\n\n⏳ 正在处理 · {elapsed} 秒"
            else:
                text = f"⏳ {active['progress']} · {elapsed} 秒"
            # Once quiet, refresh at most every ten seconds.
            if text != previous and (stream or elapsed % 10 < 2):
                try:
                    await self.tg.edit(self.owner["chat_id"], active["live_message"], self.safe(text))
                    previous = text
                except TelegramError:
                    pass
            if elapsed % 5 < 2:
                with contextlib.suppress(TelegramError):
                    await self.tg.call("sendChatAction", chat_id=self.owner["chat_id"], action="typing")

    async def events(self):
        while True:
            event = await self.rpc.events.get()
            method, params = event.get("method", ""), event.get("params", {})
            if method == "bridge/serverExited":
                if self.active and not self.active["finished"].done():
                    self.active["finished"].set_exception(ConnectionError("Codex app-server 已退出"))
                raise ConnectionError("Codex app-server 已退出")
            if method == "serverRequest/resolved":
                request = self.server_requests.get(params.get("requestId"))
                if request and request["thread_id"] == params.get("threadId"):
                    request["resolved"] = True
                    request["task"].cancel()
                continue
            if "id" in event and method:
                task = asyncio.create_task(self.server_request(event))
                self.prompt_tasks.add(task)
                task.add_done_callback(self.prompt_tasks.discard)
                continue
            active = self.active
            if not active or params.get("threadId") != active.get("thread_id"):
                continue
            if params.get("turnId") and active.get("turn_id") and params["turnId"] != active["turn_id"]:
                continue
            active["last_event"] = time.time()
            if method == "turn/started":
                active["turn_id"] = params["turn"]["id"]
                self.store.update(active["id"], turn_id=active["turn_id"])
            elif method == "item/agentMessage/delta":
                item_id = params["itemId"]
                active["items"][item_id] = active["items"].get(item_id, "") + params["delta"]
                active["stream"] = active["items"][item_id]
            elif method == "item/started":
                item = params["item"]
                kind = item["type"]
                if kind == "commandExecution":
                    active["progress"] = "正在执行：" + (item.get("command", "命令").splitlines() or ["命令"])[0][:180]
                    active["stream"] = ""
                elif kind == "fileChange":
                    active["progress"] = "正在修改文件"
                    active["stream"] = ""
                elif kind == "mcpToolCall":
                    active["progress"] = "正在调用工具：" + item.get("tool", "")
            elif method == "item/completed" and params["item"]["type"] == "agentMessage":
                active["messages"].append(params["item"])
                active["stream"] = params["item"].get("text", "")
            elif method == "turn/plan/updated":
                current = next((x["step"] for x in params.get("plan", []) if x.get("status") == "inProgress"), None)
                if current:
                    active["progress"] = current[:200]
            elif method == "turn/completed":
                if not active["finished"].done():
                    active["finished"].set_result(params["turn"])

    async def server_request(self, event):
        request_id = event["id"]
        request = {"task": asyncio.current_task(), "thread_id": event.get("params", {}).get("threadId"), "resolved": False}
        self.server_requests[request_id] = request
        try:
            # Keep whole forms together: only one free-text prompt can own incoming replies.
            async with self.interaction_lock:
                await self._server_request(event)
        finally:
            if self.server_requests.get(request_id) is request:
                self.server_requests.pop(request_id, None)

    async def _server_request(self, event):
        request_id, method, params = event["id"], event["method"], event.get("params", {})
        try:
            if not self.owner or params.get("threadId") != self.store.get("thread_id"):
                await self.rpc.send({"id": request_id, "error": {"code": -32601, "message": "No authorized Telegram session"}})
                return
            if params.get("turnId") and self.active and self.active.get("turn_id") and params["turnId"] != self.active["turn_id"]:
                await self.rpc.send({"id": request_id, "error": {"code": -32000, "message": "Turn is no longer active"}})
                return
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}:
                detail = params.get("reason") or "Codex 请求你确认此操作。"
                if params.get("command"):
                    detail += "\n\n" + params["command"]
                if params.get("grantRoot"):
                    detail += "\n写入目录：" + params["grantRoot"]
                if params.get("permissions"):
                    detail += "\n权限：" + json.dumps(params["permissions"], ensure_ascii=False)
                allowed = params.get("availableDecisions")
                choices = [("允许这次", "accept"), ("拒绝", "decline")]
                if allowed is not None:
                    choices = [(label, value) for label, value in choices if value in allowed]
                if not choices:
                    await self.rpc.send({"id": request_id, "result": {"decision": "decline"}})
                    return
                decision = await self.ask("需要确认\n\n" + detail, choices)
                if method == "item/permissions/requestApproval":
                    result = {"permissions": params["permissions"] if decision == "accept" else {}, "scope": "turn"}
                else:
                    result = {"decision": decision or "decline"}
            elif method == "item/tool/requestUserInput":
                result = {"answers": {}}
                for question in params.get("questions", []):
                    options = question.get("options") or []
                    detail = question["question"]
                    for option in options:
                        detail += "\n• " + option["label"] + "：" + option.get("description", "")
                    answer = await self.ask(detail + "\n\n可点击选项，或直接回复文字。", [(option["label"], option["label"]) for option in options], free_text=True)
                    result["answers"][question["id"]] = {"answers": [answer] if answer else []}
            elif method == "item/tool/call" and params.get("tool") == "telegram_send_file":
                try:
                    arguments = params["arguments"]
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    await self.send_file(arguments["path"], arguments.get("caption", ""))
                    result = {"success": True, "contentItems": [{"type": "inputText", "text": "文件已发送到当前用户的 Telegram 私聊。"}]}
                except Exception as error:
                    result = {"success": False, "contentItems": [{"type": "inputText", "text": self.safe(error)}]}
            elif method == "mcpServer/elicitation/request":
                result = await Elicitation(self).handle(params)
            else:
                await self.rpc.send({"id": request_id, "error": {"code": -32601, "message": "Unsupported Telegram client request"}})
                return
            await self.rpc.send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            if not self.server_requests.get(request_id, {}).get("resolved"):
                with contextlib.suppress(Exception):
                    response = {"result": {"action": "cancel", "content": None}} if method == "mcpServer/elicitation/request" else {"error": {"code": -32000, "message": "Turn cancelled"}}
                    await self.rpc.send({"id": request_id, **response})
            raise
        except Exception as error:
            LOG.warning("Server request failed: %s", type(error).__name__)
            with contextlib.suppress(Exception):
                await self.rpc.send({"id": request_id, "error": {"code": -32000, "message": self.safe(error)}})

    def prompt_keyboard(self, token, prompt):
        keyboard = []
        for i, (label, value) in enumerate(prompt["choices"]):
            if prompt.get("multiple") and not isinstance(value, Action):
                label = ("☑ " if i in prompt["selected"] else "☐ ") + label
            keyboard.append([{"text": self.safe(label)[:55], "callback_data": f"prompt:{token}:{i}"}])
        return prompt.get("links", []) + keyboard

    async def ask(self, text, choices, free_text=False, multiple=False, links=None):
        token = secrets.token_hex(6)
        future = asyncio.get_running_loop().create_future()
        prompt = {"created_at": time.time(), "future": future, "choices": choices, "multiple": multiple, "selected": set(), "links": [[{"text": label, "url": url}] for label, url in (links or [])]}
        self.prompts[token] = prompt
        keyboard = self.prompt_keyboard(token, prompt)
        if free_text:
            self.current_question = future
        if self.active:
            self.active["progress"] = "等待你回复" if free_text else "等待你审批"
            self.active["stream"] = ""
        try:
            parts = chunks(self.safe(text), 3300)
            for index, part in enumerate(parts):
                # Long approval commands must be shown completely before presenting consent.
                sent = await self.say(part, keyboard=keyboard if index == len(parts) - 1 else None)
            self.prompts[token]["message_id"] = sent["message_id"]
            self.prompts[token]["text"] = parts[-1]
            if self.gateway:
                history = self.store.get("gateway_prompt_messages", [])
                self.store.set("gateway_prompt_messages", (history + [sent["message_id"]])[-200:])
            value = await future
            with contextlib.suppress(TelegramError):
                label = next((label for label, choice in choices if choice == value), str(value))
                await self.tg.edit(self.owner["chat_id"], sent["message_id"], parts[-1] + "\n\n已回复：" + chunks(self.safe(label), 150)[0])
            return value
        except asyncio.CancelledError:
            if prompt.get("message_id"):
                with contextlib.suppress(TelegramError):
                    await self.tg.edit(self.owner["chat_id"], prompt["message_id"], parts[-1] + "\n\n此问题已结束，按钮已失效。")
            raise
        finally:
            self.prompts.pop(token, None)
            if self.current_question is future:
                self.current_question = None
            if self.active:
                self.active["progress"] = "正在继续处理"

    async def answer_callback(self, **params):
        try:
            return await self.tg.call("answerCallbackQuery", **params)
        except TelegramError as error:
            if not self.gateway or error.code not in (0, 400):
                raise
            # VPS already acknowledges the tap; a late callback may expire after pause.

    async def callback(self, query):
        message = query.get("message", {})
        candidate = {**message, "from": query.get("from", {})}
        if not self.authorized(candidate):
            return
        match = re.fullmatch(r"prompt:([a-f0-9]+):(\d+)", query.get("data", ""))
        prompt = self.prompts.get(match[1]) if match else None
        if not prompt or int(match[2]) >= len(prompt["choices"]) or prompt["future"].done():
            await self.answer_callback(callback_query_id=query["id"], text="此按钮已失效，请查看最新消息。")
            return
        # The token must belong to the exact owner chat and the displayed prompt.
        if message.get("message_id") != prompt.get("message_id"):
            return
        self.last_input = time.time()
        self.store.set("last_input", self.last_input)
        self.idle_notice = False
        index = int(match[2])
        value = prompt["choices"][index][1]
        if prompt.get("multiple") and not isinstance(value, Action):
            if index in prompt["selected"]:
                prompt["selected"].remove(index)
            else:
                prompt["selected"].add(index)
            await self.answer_callback(callback_query_id=query["id"], text="已更新选择")
            await self.tg.edit(self.owner["chat_id"], prompt["message_id"], prompt["text"], keyboard=self.prompt_keyboard(match[1], prompt))
            return
        if prompt.get("multiple") and value is Action.DONE:
            value = [prompt["choices"][i][1] for i in sorted(prompt["selected"])]
        prompt["future"].set_result(value)
        await self.answer_callback(callback_query_id=query["id"], text="已收到")

    def output_path(self, value):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path("/data/workspace") / path
        path = path.resolve(strict=True)
        if not any(path.is_relative_to(Path(root)) for root in OUTPUT_ROOTS):
            raise ValueError("只能发送 /data/workspace 或 /data/home 下的工作文件。")
        forbidden = {".codex", ".aws", ".ssh", ".gnupg", ".config", ".env", "secrets.json", "state.sqlite3"}
        if any(part in forbidden or part.startswith(".env.") for part in path.parts) or path.is_relative_to(ROOT):
            raise ValueError("此路径属于凭据、配置或连接服务，不能作为附件发送。")
        if not path.is_file() or path.stat().st_size > 45 * 1024 * 1024:
            raise ValueError("请发送普通文件，大小不超过 45 MB。")
        return path

    async def send_file(self, value, caption=""):
        path = self.output_path(value)
        await self.tg.call("sendDocument", chat_id=self.owner["chat_id"], _multipart=(path, self.safe(caption)))

    async def lifecycle(self):
        previous = time.time()
        while True:
            await asyncio.sleep(5)
            now = time.time()
            if now - previous > 90:
                # VM pause preserves memory. Retain the running Codex process and let it reconnect.
                self.run_epoch = now
                self.cap_notice = False
                self.last_input = now
                self.idle_notice = False
                LOG.info("Detected execution pause; resuming connection")
                with contextlib.suppress(TelegramError):
                    await self.say("检测到休眠或执行暂停，连接已恢复。对话和等待队列已保留。" + ("\n正在恢复当前任务；可用 /status 查看，或用 /cancel 取消。" if self.active else ""))
                if self.active:
                    self.active["progress"] = "暂停后正在恢复任务"
            previous = now
            if not self.owner:
                continue
            if not self.active and now - self.last_input >= 25 * 60 and not self.idle_notice:
                self.idle_notice = True
                with contextlib.suppress(TelegramError):
                    await self.say("25 分钟未收到 Telegram 操作，机器可能按平台空闲规则休眠。对话记录会保留。" + ("可在此 bot 使用 /resume 唤醒。" if self.gateway else "休眠后请在 instabox 面板唤醒。"))
            remaining = 8 * 3600 - (now - self.run_epoch)
            if remaining <= 10 * 60 and not self.cap_notice:
                self.cap_notice = True
                with contextlib.suppress(TelegramError):
                    await self.say("本轮运行时间预计已接近 8 小时上限，虚拟机可能很快休眠。请及时保存工作；休眠后可能需要等待冷却，" + ("再在此 bot 使用 /resume 唤醒。" if self.gateway else "再在 instabox 面板唤醒。") + "这个时间是本机估计，以平台显示为准。")


async def main():
    os.umask(0o077)
    config = json.loads((ROOT / "secrets.json").read_text())
    (ROOT / "uploads").mkdir(mode=0o700, exist_ok=True)
    bridge = Bridge(config)
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, bridge.stopping.set)
    await bridge.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except Exception as error:
        # Log only the exception class to prevent credential-bearing URLs leaking.
        LOG.error("Bridge stopped: %s", type(error).__name__)
        raise SystemExit(1)
