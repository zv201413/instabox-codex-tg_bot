"""Isolated container smoke: fake machine, no external network or credentials."""
import asyncio
import json
import os
import sys
sys.path.insert(0, '/app')
from zoneinfo import ZoneInfo
import urllib.request
from controller.agent_api import AgentApiServer
from controller.config import ControllerConfig
from controller.db import Database
from controller.scheduler import LeaseScheduler
from controller.telegram_bot import TelegramBot


class Machine:
    def __init__(self):
        self.state, self.calls = 'running', []
    async def resolve_machine_id(self, target): return target
    async def get_machine(self, target): return dict(id=target, name='fake', state=self.state)
    async def put_lease(self, *args): self.calls.append('lease'); return {}
    async def delete_lease(self, *args): self.calls.append('release'); return True
    async def suspend_machine(self, *args):
        self.calls.append('suspend'); self.state='suspended'; return dict(id='fake_operation')


async def main():
    assert os.getuid() == 10001
    ZoneInfo('Asia/Shanghai')
    cfg = ControllerConfig(machine_id='m_test', api_key='fake', agent_secret='s'*32,
        tg_allowed_users=[7], keep_mode='manual', max_hold_seconds=28800, agent_api_port=0,
        db_path='/data/smoke.sqlite3')
    db=Database(cfg.db_path)
    machine=Machine()
    scheduler=LeaseScheduler(cfg,db,machine)
    await scheduler.initialize()
    api=AgentApiServer(cfg,db,scheduler)
    await api.start()
    try:
        port=api.server.sockets[0].getsockname()[1]
        def health():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz') as response:
                return json.load(response)
        assert (await asyncio.to_thread(health))['status']=='ok'
        bot=TelegramBot(cfg,db,scheduler)
        def event(uid,text):
            return dict(update_id=uid,message=dict(message_id=uid,text=text,chat=dict(id=7,type='private'),**{'from':dict(id=7)}))
        await bot.handle_update(event(1,'/hold 8h'))
        await scheduler._tick()
        assert 'lease' in machine.calls
        await bot.handle_update(event(2,'/sleep'))
        await scheduler._tick()
        await scheduler._tick()
        await scheduler._tick()
        assert machine.calls[-2:]==['release','suspend']
        assert db.get_state()['sleep_phase']=='done'
        assert db.get_state()['keep_mode']=='manual'
        print('Container smoke passed: UID, timezone, writable volume, SQLite, HTTP health, hold and sleep.')
    finally:
        await api.stop()


asyncio.run(main())
