import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.fail_edits = 0

    async def send(self, chat, text, reply=None, keyboard=None):
        self.sent.append((chat, text, keyboard))
        return {"message_id": len(self.sent), "chat": {"id": chat}}

    async def edit(self, *args, **kwargs):
        if self.fail_edits:
            self.fail_edits -= 1
            raise bridge.TelegramError(0, "offline")

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return {}


class FakeRPC:
    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        return {"thread": {"id": "test-thread"}, "model": "configured-model"}

    async def send(self, value):
        self.calls.append(("send", value))


def message(text, user=123, chat=123, kind="private", mid=1):
    return {"text": text, "from": {"id": user, "is_bot": False}, "chat": {"id": chat, "type": kind}, "message_id": mid}


class FormattingTests(unittest.TestCase):
    def test_long_unicode_is_complete_and_within_telegram_limit(self):
        text = ("中文😀🙂\n" * 2000) + "尾部"
        result = bridge.chunks(text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in result))
        self.assertEqual("".join(result).replace("\n", ""), text.replace("\n", ""))

    def test_markdown_escapes_html_and_code(self):
        value = bridge.render('**标题**\n<script>alert(1)</script>\n```python\na < b && c\n```')
        self.assertIn("<b>标题</b>", value)
        self.assertNotIn("<script>", value)
        self.assertIn("<pre>a &lt; b &amp;&amp; c\n</pre>", value)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(bridge, "ROOT", Path(self.directory.name))
        self.root_patch.start()
        self.client = bridge.Bridge({"telegram_token": "test-token", "pairing_code": "test-pairing", "codex_environment": {}})
        self.client.tg = FakeTelegram()
        self.client.rpc = FakeRPC()

    async def asyncTearDown(self):
        self.client.store.db.close()
        self.root_patch.stop()
        self.directory.cleanup()

    async def pair(self):
        await self.client.handle({"update_id": 1, "message": message("/start test-pairing")})

    async def test_pairing_rejects_wrong_code_and_groups(self):
        await self.client.handle({"update_id": 1, "message": message("/start wrong")})
        await self.client.handle({"update_id": 2, "message": message("/start test-pairing", kind="group")})
        self.assertIsNone(self.client.owner)
        await self.pair()
        self.assertEqual(self.client.owner["user_id"], 123)
        await self.client.handle({"update_id": 3, "message": message("/start test-pairing", user=456, chat=456)})
        self.assertEqual(self.client.owner["user_id"], 123)

    async def test_plain_start_provides_binding_instructions_without_leaking_code(self):
        await self.client.handle({"update_id": 1, "message": message("/start")})
        self.assertIsNone(self.client.owner)
        self.assertIn("尚未完成账号绑定", self.client.tg.sent[-1][1])
        self.assertNotIn("test-pairing", self.client.tg.sent[-1][1])

    async def test_owner_only_and_duplicate_messages_not_requeued(self):
        await self.pair()
        await self.client.handle({"update_id": 2, "message": message("run", user=456, chat=456)})
        await self.client.handle({"update_id": 3, "message": message("run", chat=456)})
        self.assertEqual(self.client.store.queued(), 0)
        event = {"update_id": 4, "message": message("合法任务")}
        await self.client.handle(event)
        await self.client.handle(event)
        self.assertEqual(self.client.store.queued(), 1)

    async def test_session_persists_and_running_work_not_requeued(self):
        await self.pair()
        thread = await self.client.ensure_thread()
        self.client.store.add(4, message("有副作用的任务"))
        self.client.store.update(4, status="running", thread_id=thread)
        store = bridge.Store(Path(self.directory.name) / "state.sqlite3")
        self.assertEqual(store.get("thread_id"), thread)
        self.assertEqual(store.queued(), 0)
        store.db.close()

    async def test_approval_button_rejects_other_users_and_stale_prompts(self):
        await self.pair()
        task = asyncio.create_task(self.client.ask("审批操作", [("允许", "accept"), ("拒绝", "decline")]))
        await asyncio.sleep(0)
        token = next(iter(self.client.prompts))
        mid = self.client.prompts[token]["message_id"]
        query = {"id": "cb", "data": f"prompt:{token}:0", "message": message("", mid=mid), "from": {"id": 456}}
        await self.client.callback(query)
        self.assertFalse(task.done())
        query["from"] = {"id": 123}
        query["message"]["message_id"] = 999
        await self.client.callback(query)
        self.assertFalse(task.done())
        query["message"]["message_id"] = mid
        await self.client.callback(query)
        self.assertEqual(await task, "accept")
        await self.client.callback(query)
        self.assertEqual(self.client.tg.calls[-1][0], "answerCallbackQuery")

    async def test_free_text_answers_question_instead_of_starting_task(self):
        await self.pair()
        task = asyncio.create_task(self.client.ask("选择目录", [], free_text=True))
        await asyncio.sleep(0)
        await self.client.handle({"update_id": 3, "message": message("/data/workspace/example")})
        self.assertEqual(await task, "/data/workspace/example")
        self.assertEqual(self.client.store.queued(), 0)

    async def test_cancel_during_preparation_is_remembered(self):
        await self.pair()
        self.client.active = {"started": 0}
        await self.client.handle({"update_id": 3, "message": message("/cancel")})
        self.assertTrue(self.client.active["cancel_requested"])

    async def test_long_approval_is_fully_displayed_before_button(self):
        await self.pair()
        before = len(self.client.tg.sent)
        command = "x" * 7000 + "\nDANGEROUS_TAIL"
        task = asyncio.create_task(self.client.ask(command, [("允许", "accept")]))
        await asyncio.sleep(0)
        displayed = self.client.tg.sent[before:]
        self.assertIn("DANGEROUS_TAIL", displayed[-1][1])
        self.assertTrue(all(not entry[2] for entry in displayed[:-1]))
        self.assertTrue(displayed[-1][2])
        token = next(iter(self.client.prompts))
        self.client.prompts[token]["future"].set_result("accept")
        await task

    async def test_vm_pause_preserves_active_job_and_queue(self):
        await self.pair()
        self.client.store.add(5, message("下一条任务"))
        active = {"progress": "执行中"}
        self.client.active = active
        start = 100000.0
        calls = 0

        async def interrupted_sleep(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError()

        with patch("bridge.time.time", side_effect=[start, start + 300]), patch("bridge.asyncio.sleep", interrupted_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.client.lifecycle()
        self.assertIs(self.client.active, active)
        self.assertEqual(self.client.store.queued(), 1)
        self.assertEqual(self.client.rpc.calls, [])
        self.assertEqual(self.client.run_epoch, start + 300)
        self.assertIn("连接已恢复", self.client.tg.sent[-1][1])

    async def test_delivery_failure_never_reexecutes_completed_work(self):
        await self.pair()
        self.client.store.add(3, message("生成文件"))
        self.client.store.update(3, status="done", result="已完成")
        self.client.tg.fail_edits = 1
        original_sleep = asyncio.sleep

        async def quick_sleep(seconds):
            await original_sleep(0)

        with patch("bridge.asyncio.sleep", quick_sleep):
            await self.client.deliver_result(3, "已完成", 5)
        row = self.client.store.db.execute("SELECT status,delivered FROM jobs WHERE id=3").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(self.client.store.queued(), 0)

    async def test_file_export_blocks_credentials_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'OUTPUT_ROOTS', (directory,)):
            root = Path(directory)
            credential = root / ".env"
            credential.write_text("private")
            link = root / "link"
            link.symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                self.client.output_path(str(credential))
            with self.assertRaises(ValueError):
                self.client.output_path(str(link))
            artifact = root / "report.txt"
            artifact.write_text("report")
            self.assertEqual(self.client.output_path(str(artifact)), artifact)


if __name__ == "__main__":
    unittest.main()
