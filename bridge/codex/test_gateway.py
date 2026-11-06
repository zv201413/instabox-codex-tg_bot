import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bridge
from gateway import GatewayError, GatewayInbox
from test_bridge import FakeRPC, FakeTelegram, message
from controller.agent_api import AgentApiServer
from controller.config import ControllerConfig
from controller.db import Database
from controller.scheduler import LeaseScheduler
from controller.telegram_bot import TelegramBot


class FakeGateway:
    def __init__(self, updates):
        self.updates, self.acked, self.fail_ack = updates, [], False

    def post(self, path, **payload):
        if path == '/agent/pull':
            return {'ok': True, 'updates': self.updates}
        if path == '/agent/ack':
            if self.fail_ack:
                raise GatewayError('ack interrupted')
            self.acked.extend(payload['update_ids'])
        return {'ok': True}


def update(uid, text='hello', **extra):
    return {'update_id': uid, 'message': dict(message(text, mid=uid), date=int(time.time()), **extra)}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(bridge, 'ROOT', Path(self.tmp.name))
        self.patch.start()
        self.config = dict(telegram_token='dummy', pairing_code='dummy', codex_environment={}, message_source='gateway',
                          gateway=dict(url='http://127.0.0.1:1234', secret='s'*32, machine_id='m_test', agent_id='codex-bridge'))
        self.bridge = bridge.Bridge(self.config)
        self.bridge.tg = FakeTelegram()
        self.bridge.rpc = FakeRPC()
        self.bridge.store.set('owner', {'user_id':123, 'chat_id':123})

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.patch.stop()
        self.tmp.cleanup()

    async def receive(self, updates):
        self.bridge.gateway = FakeGateway(updates)
        await self.bridge.gateway_receive_once()
        while await self.bridge.gateway_dispatch_once():
            pass

    async def test_ack_loss_redelivery_never_queues_twice(self):
        client = FakeGateway([update(10)])
        client.fail_ack = True
        self.bridge.gateway = client
        with self.assertRaises(GatewayError):
            await self.bridge.gateway_receive_once()
        self.assertEqual(self.bridge.gateway_inbox.pending()[0]['update_id'], 10)
        client.fail_ack = False
        await self.bridge.gateway_receive_once()
        await self.bridge.gateway_dispatch_once()
        await self.bridge.gateway_receive_once()
        self.assertFalse(await self.bridge.gateway_dispatch_once())
        self.assertEqual(self.bridge.store.queued(), 1)

    async def test_pause_attachments_preserve_file_ids_and_caption(self):
        await self.receive([update(11, '', photo=[{'file_id':'image-file'}], caption='image caption'),
                            update(12, '', document={'file_id':'doc-file','file_name':'a.txt'}, caption='doc caption')])
        rows = self.bridge.store.db.execute('SELECT message FROM jobs ORDER BY id').fetchall()
        self.assertEqual(json.loads(rows[0][0])['photo'][0]['file_id'], 'image-file')
        self.assertEqual(json.loads(rows[1][0])['caption'], 'doc caption')

    async def test_callback_is_dispatched_once_with_original_token(self):
        task = asyncio.create_task(self.bridge.ask('approval', [('yes','accept')]))
        await asyncio.sleep(0)
        token = next(iter(self.bridge.prompts))
        mid = self.bridge.prompts[token]['message_id']
        event = {'update_id':14, 'callback_query':dict(id='cb', data=f'prompt:{token}:0',
                    message=message('', mid=mid), **{'from':{'id':123}})}
        await self.receive([event])
        self.assertEqual(await task, 'accept')
        count = len(self.bridge.tg.calls)
        await self.receive([event])
        self.assertEqual(len(self.bridge.tg.calls), count)

    async def test_multiselect_updates_even_when_callback_ack_expired(self):
        task = asyncio.create_task(self.bridge.ask('choose', [('A','a'),('B','b'),('done',bridge.Action.DONE)], multiple=True))
        await asyncio.sleep(0)
        token = next(iter(self.bridge.prompts))
        prompt = self.bridge.prompts[token]
        async def expired(method, **params):
            raise bridge.TelegramError(400, 'query too old')
        self.bridge.tg.call = expired
        event = {'update_id':15, 'callback_query':dict(id='cb', data=f'prompt:{token}:0',
                    message=message('', mid=prompt['message_id']), **{'from':{'id':123}})}
        await self.receive([event])
        self.assertEqual(prompt['selected'], {0})
        event = {**event, 'update_id':16, 'callback_query':{**event['callback_query'], 'data':f'prompt:{token}:2'}}
        await self.receive([event])
        self.assertEqual(await task, ['a'])

    async def test_form_text_uses_existing_question_future(self):
        task = asyncio.create_task(self.bridge.ask('value', [], free_text=True))
        await asyncio.sleep(0)
        await self.receive([update(17, 'typed answer')])
        self.assertEqual(await task, 'typed answer')
        self.assertEqual(self.bridge.store.queued(), 0)

    async def test_text_for_previous_question_is_rejected(self):
        task = asyncio.create_task(self.bridge.ask('new question', [], free_text=True))
        await asyncio.sleep(0)
        self.bridge.gateway_inbox.ingest([update(18, 'old answer')], {'prompt_token':'old'})
        await self.bridge.gateway_dispatch_once()
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_old_path_answer_is_not_treated_as_a_command(self):
        task = asyncio.create_task(self.bridge.ask('new question', [], free_text=True))
        await asyncio.sleep(0)
        self.bridge.gateway_inbox.ingest([update(19, '/data/workspace/old')], {'prompt_token':'old'})
        await self.bridge.gateway_dispatch_once()
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_controls_dispatch_when_twenty_jobs_queued(self):
        for uid in range(100,120):
            self.bridge.store.add(uid, message('task'))
        self.bridge.active = dict(id=50, started=time.time()-10)
        await self.receive([update(21, '/cancel')])
        self.assertTrue(self.bridge.active['cancel_requested'])
        self.assertEqual(self.bridge.store.queued(), 20)

    async def test_reply_to_ended_form_never_becomes_new_task(self):
        self.bridge.store.set('gateway_prompt_messages', [555])
        await self.receive([update(23, 'old form answer', reply_to_message={'message_id':555})])
        self.assertEqual(self.bridge.store.queued(), 0)

    async def test_cancel_for_old_task_never_cancels_new_task(self):
        self.bridge.active = dict(id=50, started=time.time()-10)
        self.bridge.gateway_inbox.ingest([update(22, '/cancel')], {'task_id':'old'})
        await self.bridge.gateway_dispatch_once()
        self.assertNotIn('cancel_requested', self.bridge.active)

    async def test_transport_selection_has_no_old_polling(self):
        calls = []
        async def loop(name):
            calls.append(name)
            await self.bridge.stopping.wait()
        self.bridge.rpc.start = lambda: asyncio.sleep(0)
        self.bridge.rpc.close = lambda: asyncio.sleep(0)
        async def telegram_call(method, **params):
            return {'username':'test'} if method == 'getMe' else {}
        self.bridge.tg.call = telegram_call
        for name in ['poll','gateway_poll','gateway_heartbeat','gateway_dispatch','worker','events','lifecycle']:
            setattr(self.bridge, name, lambda name=name: loop(name))
        running = asyncio.create_task(self.bridge.run())
        for _ in range(20):
            await asyncio.sleep(0)
            if 'gateway_dispatch' in calls:
                break
        self.bridge.stopping.set()
        await running
        self.assertNotIn('poll', calls)
        self.assertTrue({'gateway_poll','gateway_heartbeat','gateway_dispatch'} <= set(calls))
        self.assertEqual(self.bridge.store.get('owner')['user_id'], 123)

    async def test_real_http_gateway_delivers_to_actual_bridge(self):
        class Machine:
            async def resolve_machine_id(self, target): return target
            async def get_machine(self, target): return dict(id=target, name='test', state='running')
        cfg = ControllerConfig(machine_id='m_test', api_key='dummy', agent_secret='s'*32,
            tg_allowed_users=[123], db_path=self.tmp.name+'/controller.db', agent_api_port=0)
        db = Database(cfg.db_path)
        scheduler = LeaseScheduler(cfg, db, Machine())
        await scheduler.initialize()
        api = AgentApiServer(cfg, db, scheduler)
        await api.start()
        port = api.server.sockets[0].getsockname()[1]
        self.bridge.gateway = bridge.GatewayClient(f'http://127.0.0.1:{port}', 's'*32, 'm_test')
        self.bridge.rpc_ready = True
        try:
            await TelegramBot(cfg, db, scheduler).handle_update(update(25, 'real HTTP delivery'))
            await self.bridge.gateway_heartbeat_once()
            self.assertEqual(await self.bridge.gateway_receive_once(), 1)
            self.assertEqual(db.get_queue_stats()['acked'], 1)
            await self.bridge.gateway_dispatch_once()
            self.assertEqual(self.bridge.store.queued(), 1)
            self.assertEqual(db.pull_pending_updates(), [])
            for uid in range(100,119):
                self.bridge.store.add(uid, message('queued task'))
            task = asyncio.create_task(self.bridge.ask('form with full queue', [], free_text=True))
            await asyncio.sleep(0)
            await TelegramBot(cfg, db, scheduler).handle_update(update(26, 'answer despite full queue'))
            await self.bridge.gateway_heartbeat_once()
            self.assertEqual(self.bridge.gateway_capacity(), 0)
            self.assertEqual(await self.bridge.gateway_receive_once(), 1)
            await self.bridge.gateway_dispatch_once()
            self.assertEqual(await task, 'answer despite full queue')
            self.assertEqual(self.bridge.store.queued(), 20)
        finally:
            await api.stop()


if __name__ == '__main__':
    unittest.main()
