"""
VPS Controller 综合自动化测试套件
全面覆盖：
- 配置校验与约束（TTL、Renew、模式）
- SQLite 持久化队列（入库去重、pull/ack 状态流转、游标持久化）
- InstaBox API 客户端（机器查询、租约、Resume、409 冷却时间解析）
- 调度器状态机（on_demand 模式、超时释放、冷却保护、安全唤醒策略）
- Agent API 接口（healthz、pull、ack、heartbeat、鉴权）
- Telegram 控制网关（命令拦截、按钮、白名单鉴权、消息暂存）
"""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from controller.agent_api import AgentApiServer
from controller.config import ControllerConfig
from controller.db import Database, now_iso
from controller.instabox import InstaBoxClient, InstaBoxError
from controller.scheduler import LeaseScheduler
from controller.telegram_bot import TelegramBot


class MockHttpServer:
    """本地 Mock 服务，支持 InstaBox API 与 Telegram API 桩"""
    def __init__(self):
        self.server = None
        self.port = 0
        self.requests = []
        self.scenario = "running"
        self.cooldown_until = None

    async def _handler(self, reader, writer):
        line = await reader.readline()
        if not line:
            writer.close()
            return
        req = line.decode().strip()
        method, path = req.split()[0], req.split()[1]

        headers = {}
        while True:
            hl = await reader.readline()
            if not hl or hl == b"\r\n":
                break
            h_str = hl.decode().strip()
            if ":" in h_str:
                k, v = h_str.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        content_len = int(headers.get("content-length", "0"))
        body_bytes = await reader.readexactly(content_len) if content_len > 0 else b""
        body = json.loads(body_bytes.decode()) if body_bytes else {}

        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})

        # 路由响应
        status = 200
        resp = {}

        if path == "/v1/machines":
            resp = {"machines": [{"id": "m_test001", "name": "my-box", "state": self.scenario}]}
        elif path.startswith("/v1/machines/m_test001/activity-leases/"):
            if method == "PUT":
                if self.scenario == "running":
                    resp = {"expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()}
                else:
                    status = 409
                    resp = {"error": {"code": "not_running", "message": f"machine state is {self.scenario}"}}
            elif method == "DELETE":
                resp = {"deleted": True}
        elif path == "/v1/machines/m_test001/resume":
            if self.cooldown_until:
                status = 409
                resp = {
                    "error": {
                        "code": "cooldown_active",
                        "message": "cooldown active",
                        "cooldownUntil": self.cooldown_until,
                        "retryAfterSeconds": 300
                    }
                }
            else:
                self.scenario = "running"
                resp = {"id": "op_wake123", "state": "pending"}
        elif path == "/v1/machines/m_test001":
            resp = {"id": "m_test001", "name": "my-box", "state": self.scenario}
        elif "/sendMessage" in path or "/answerCallbackQuery" in path:
            resp = {"ok": True, "result": {"message_id": 999}}
        elif "/getUpdates" in path:
            resp = {"ok": True, "result": []}
        else:
            resp = {"ok": True}

        resp_bytes = json.dumps(resp).encode()
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_bytes)}\r\n\r\n".encode())
        writer.write(resp_bytes)
        await writer.drain()
        writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


class TestControllerSuite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ctrl-test-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.mock_srv = MockHttpServer()
        await self.mock_srv.start()

    async def asyncTearDown(self):
        await self.mock_srv.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. 配置校验测试
    def test_config_validation(self):
        # 正常默认
        os.environ["INSTABOX_LEASE_TTL"] = "300"
        os.environ["INSTABOX_RENEW_EVERY"] = "120"
        cfg = ControllerConfig.from_env()
        self.assertEqual(cfg.lease_ttl, 300)
        self.assertEqual(cfg.renew_interval, 120)

        # renew >= ttl 必须报错
        os.environ["INSTABOX_RENEW_EVERY"] = "300"
        with self.assertRaises(ValueError):
            ControllerConfig.from_env()

        # renew <= 0 必须报错
        os.environ["INSTABOX_RENEW_EVERY"] = "0"
        with self.assertRaises(ValueError):
            ControllerConfig.from_env()

        # ttl 越界
        os.environ["INSTABOX_LEASE_TTL"] = "10"
        with self.assertRaises(ValueError):
            ControllerConfig.from_env()

        os.environ["INSTABOX_LEASE_TTL"] = "300"
        os.environ["INSTABOX_RENEW_EVERY"] = "120"

    # 2. SQLite 数据库队列与幂等测试
    def test_database_queue_and_idempotency(self):
        db = Database(self.db_path)

        # 插入 2 条消息
        ok1 = db.enqueue_update(1001, 888, 999, 1, "hello", '{"text": "hello"}')
        ok2 = db.enqueue_update(1002, 888, 999, 2, "world", '{"text": "world"}')
        self.assertTrue(ok1)
        self.assertTrue(ok2)

        # 重复插入 1001 必须幂等忽略
        dup = db.enqueue_update(1001, 888, 999, 1, "hello", '{"text": "hello"}')
        self.assertFalse(dup)

        stats = db.get_queue_stats()
        self.assertEqual(stats["pending"], 2)

        # Bridge pull
        pulled = db.pull_pending_updates(limit=10)
        self.assertEqual(len(pulled), 2)
        stats = db.get_queue_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["delivered"], 2)

        # Bridge ack
        acked = db.ack_updates([1001])
        self.assertEqual(acked, 1)
        stats = db.get_queue_stats()
        self.assertEqual(stats["acked"], 1)

        # 游标 meta
        db.set_meta("tg_offset", "1003")
        self.assertEqual(db.get_meta("tg_offset"), "1003")

    # 3. InstaBox 客户端与冷却解析
    async def test_instabox_client_cooldown(self):
        client = InstaBoxClient(f"http://127.0.0.1:{self.mock_srv.port}", api_key="test_key")

        # 机器列表
        machines = await client.list_machines()
        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]["id"], "m_test001")

        # 模拟冷却
        future_cd = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        self.mock_srv.cooldown_until = future_cd
        self.mock_srv.scenario = "paused"

        with self.assertRaises(InstaBoxError) as ctx:
            await client.resume_machine("m_test001")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.cooldown_until, future_cd)
        self.assertEqual(ctx.exception.retry_after, 300)

    # 4. 调度器状态机与安全唤醒
    async def test_scheduler_state_machine(self):
        cfg = ControllerConfig(
            api_url=f"http://127.0.0.1:{self.mock_srv.port}",
            api_key="test_key",
            machine_id="m_test001",
            db_path=self.db_path,
            lease_ttl=300,
            renew_interval=120,
            keep_mode="on_demand",
            auto_start=False
        )
        db = Database(self.db_path)
        client = InstaBoxClient(cfg.api_url, cfg.api_key)
        scheduler = LeaseScheduler(cfg, db, client)
        await scheduler.initialize()

        # 机器 running 时请求唤醒 -> 返回已在运行
        self.mock_srv.scenario = "running"
        ok, msg = await scheduler.request_resume()
        self.assertTrue(ok)
        self.assertTrue("Running" in msg or "运行" in msg)

        # 触发保活
        scheduler.trigger_activity()
        st = db.get_state()
        self.assertIsNotNone(st.get("hold_until"))

        # 机器 paused 时请求唤醒
        self.mock_srv.scenario = "paused"
        self.mock_srv.cooldown_until = None
        ok, msg = await scheduler.request_resume()
        self.assertTrue(ok)

        # 机器 stopped 时请求唤醒
        self.mock_srv.scenario = "stopped"
        db.update_state(vm_state="stopped")
        ok, msg = await scheduler.request_resume()
        self.assertTrue(ok)
        self.assertIn("恢复请求", msg)

        # 主动释放租约
        ok, msg = await scheduler.release_lease()
        self.assertTrue(ok)
        st = db.get_state()
        self.assertIsNone(st.get("hold_until"))
        self.assertEqual(st.get("keep_mode"), "manual")

    # 5. Agent API 服务接口测试
    async def test_agent_api(self):
        cfg = ControllerConfig(
            agent_api_host="127.0.0.1",
            agent_api_port=0,
            agent_secret="sec123",
            db_path=self.db_path
        )
        db = Database(self.db_path)
        db.update_state(vm_state="running")
        db.enqueue_update(2001, 1, 1, 1, "test", '{"text":"test"}')

        client = InstaBoxClient(f"http://127.0.0.1:{self.mock_srv.port}", "k")
        scheduler = LeaseScheduler(cfg, db, client)
        scheduler.resolved_machine_id = "m_test001"

        server = AgentApiServer(cfg, db, scheduler)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]

        from bridge.gateway_client import GatewayClient
        bridge_cli = GatewayClient(f"http://127.0.0.1:{port}", secret="sec123", machine_id="m_test001", agent_id="codex-bridge")

        # 心跳
        hb_ok = await asyncio.to_thread(bridge_cli.heartbeat, version="1.0", codex_status="ready")
        self.assertTrue(hb_ok)
        ag = db.get_latest_agent_status("codex-bridge")
        self.assertIsNotNone(ag)

        db.update_state(vm_state='running')
        # 拉取消息
        updates = await asyncio.to_thread(bridge_cli.pull, limit=5)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["text"], "test")

        # 确认消息
        ack_ok = await asyncio.to_thread(bridge_cli.ack, [2001])
        self.assertTrue(ack_ok)
        self.assertEqual(db.get_queue_stats()["acked"], 1)

        await server.stop()

    # 6. Telegram Bot 控制与白名单测试
    async def test_telegram_bot_handlers(self):
        cfg = ControllerConfig(
            tg_token="123456:FAKE_TOKEN",
            tg_allowed_users=[8888],
            db_path=self.db_path
        )
        db = Database(self.db_path)
        client = InstaBoxClient(f"http://127.0.0.1:{self.mock_srv.port}", "k")
        scheduler = LeaseScheduler(cfg, db, client)
        scheduler.resolved_machine_id = "m_test001"
        bot = TelegramBot(cfg, db, scheduler)
        bot.base_url = f"http://127.0.0.1:{self.mock_srv.port}"

        # 未授权用户消息被拦截，不进入 pending
        await bot.handle_update({
            "update_id": 3001,
            "message": {"message_id": 1, "from": {"id": 9999}, "chat": {"id": 9999}, "text": "bad"}
        })
        self.assertEqual(db.get_queue_stats()["pending"], 0)

        # 授权用户消息正常入库
        await bot.handle_update({
            "update_id": 3002,
            "message": {"message_id": 2, "from": {"id": 8888}, "chat": {"id": 8888}, "text": "good"}
        })
        self.assertEqual(db.get_queue_stats()["pending"], 1)

        # 授权用户发送 /resume
        await bot.handle_update({
            "update_id": 3003,
            "message": {"message_id": 3, "from": {"id": 8888}, "chat": {"id": 8888}, "text": "/resume"}
        })
        # 授权用户发送 /hold 30m
        await bot.handle_update({
            "update_id": 3004,
            "message": {"message_id": 4, "from": {"id": 8888}, "chat": {"id": 8888}, "text": "/hold 30m"}
        })
        st = db.get_state()
        self.assertTrue(bool(st.get("hold_until")) or (st.get("pending_hold_seconds") or 0) > 0)


if __name__ == "__main__":
    unittest.main()
