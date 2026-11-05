"""Behavior regressions for durable intake, release, lifecycle and Bridge handoff."""
import asyncio
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from unittest.mock import patch

from bridge.gateway_client import GatewayClient
from bridge.inbox import GatewayInbox, interaction_is_current
from controller.agent_api import AgentApiServer
from controller.config import ControllerConfig
from controller.db import Database
from controller.instabox import InstaBoxClient, InstaBoxError
from controller.main import supervise
from controller.scheduler import LeaseScheduler, iso, parse_iso
from controller.telegram_bot import TelegramBot


class FakeClient:
    def __init__(self):
        self.state = 'running'
        self.auto = None
        self.cooldown = None
        self.resume_error = None
        self.calls = []
        self.operation = {'state':'running'}
    async def resolve_machine_id(self, target):
        return target
    async def get_machine(self, mid):
        self.calls.append(('get',mid))
        return {'id':mid,'name':'test','state':self.state,'autoSuspendedAt':self.auto,'cooldownUntil':self.cooldown}
    async def resume_machine(self, mid, idempotency_key=None):
        self.calls.append(('resume',idempotency_key))
        if self.resume_error:
            raise self.resume_error
        return {'id':'op_1','state':'queued'}
    async def start_machine(self, mid, idempotency_key=None):
        self.calls.append(('start',idempotency_key))
        return {'id':'op_start','state':'queued'}
    async def get_operation(self, op):
        self.calls.append(('operation',op))
        return self.operation
    async def put_lease(self, *args):
        self.calls.append(('put',args))
        return {'expiresAt':iso(datetime.now(timezone.utc)+timedelta(seconds=300))}
    async def delete_lease(self, *args):
        self.calls.append(('delete',args))
        return True


def message(uid, text='hello', date=None):
    return {'update_id':uid,'message':{'message_id':uid,'from':{'id':7},'chat':{'id':7,'type':'private'},'text':text,'date':date or int(datetime.now(timezone.utc).timestamp())}}


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ControllerConfig(machine_id='m_test',api_key='dummy',agent_secret='s'*32,tg_allowed_users=[7],db_path=self.tmp.name+'/controller.db')
        self.db = Database(self.cfg.db_path)
        self.client = FakeClient()
        self.scheduler = LeaseScheduler(self.cfg,self.db,self.client)
        await self.scheduler.initialize()
        self.bot = TelegramBot(self.cfg,self.db,self.scheduler)
    async def asyncTearDown(self):
        self.tmp.cleanup()
    def calls(self, name):
        return [value for kind,value in self.client.calls if kind==name]
    async def pause(self, automatic=True):
        self.client.state = 'suspended'
        self.client.auto = iso(datetime.now(timezone.utc)) if automatic else None
        await self.scheduler._tick()

    async def test_authorized_bridge_callback_is_persisted_and_replayed(self):
        update = {'update_id':101,'callback_query':{'id':'cb','from':{'id':7},'message':{'message_id':1,'chat':{'id':7}},'data':'prompt:token:0'}}
        await self.bot.handle_update(update)
        self.assertEqual(self.db.pull_pending_updates()[0],update)
        restarted = Database(self.cfg.db_path)
        self.assertEqual(restarted.pull_pending_updates()[0],update)
        restarted.ack_updates([101])
        self.assertEqual(restarted.pull_pending_updates(),[])

    async def test_batch_and_offset_rollback_together(self):
        updates = [message(1),{'update_id':'invalid'}]
        with self.assertRaises(ValueError):
            self.db.ingest_batch(updates)
        self.assertEqual(self.db.get_queue_stats()['total'],0)
        self.assertIsNone(self.db.get_meta('tg_offset'))

    async def test_save_failure_never_confirms_telegram(self):
        self.bot._call_tg_api = lambda *args, **kwargs: {'ok':True,'result':[message(10)]}
        with patch.object(self.db,'ingest_batch',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                await self.bot.run_poll_loop()
        self.assertIsNone(self.db.get_meta('tg_offset'))

    async def test_commands_are_atomic_and_update_id_deduplicated(self):
        await self.bot.handle_update(message(2,'/hold 30m'))
        first = self.db.get_state()['hold_until']
        await self.bot.handle_update(message(2,'/hold 60m'))
        self.assertEqual(first,self.db.get_state()['hold_until'])
        self.assertEqual(self.db.next_reply(1e20)['id'],'update:2')

    async def test_online_interaction_extends_hold(self):
        await self.bot.handle_update(message(3))
        self.assertIsNotNone(self.db.get_state()['hold_until'])
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('put')),1)

    async def test_manual_messages_and_resume_do_not_create_hold(self):
        self.scheduler.set_mode('manual')
        await self.bot.handle_update(message(3))
        await self.bot.handle_update(message(4,'/resume'))
        self.assertIsNone(self.db.get_state()['hold_until'])
        await self.scheduler._tick()
        self.assertEqual(self.calls('put'),[])

    async def test_release_cannot_be_overridden_by_heartbeat(self):
        await self.bot.handle_update(message(3,'/hold 60m'))
        await self.scheduler._tick()
        await self.scheduler.release_lease()
        self.db.record_heartbeat(self.cfg.agent_id,'1','busy','task',10)
        await self.scheduler._tick()
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('put')),1)
        self.assertIsNone(self.db.get_state()['hold_until'])
        self.assertIsNone(self.db.get_state()['lease_expires_at'])
        self.assertEqual(self.db.get_state()['keep_mode'],'manual')

    async def test_release_waits_for_inflight_put_then_deletes(self):
        await self.bot.handle_update(message(3,'/hold 60m'))
        started, finish = asyncio.Event(), asyncio.Event()
        original = self.client.put_lease
        async def slow(*args):
            started.set()
            await finish.wait()
            return await original(*args)
        self.client.put_lease = slow
        tick = asyncio.create_task(self.scheduler._tick())
        await started.wait()
        release = asyncio.create_task(self.scheduler.release_lease())
        await asyncio.sleep(0)
        self.assertFalse(release.done())
        finish.set()
        await tick
        await release
        await self.scheduler._tick()
        self.assertEqual([kind for kind,_ in self.client.calls if kind in ('put','delete')],['put','delete'])
        self.assertIsNone(self.db.get_state()['lease_expires_at'])

    async def test_automatic_chat_wake_respects_manual_pause(self):
        await self.pause(automatic=False)
        await self.bot.handle_update(message(4))
        await self.scheduler._tick()
        self.assertEqual(self.calls('resume'),[])
        await self.bot.handle_update(message(5,'/resume'))
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('resume')),1)

    async def test_hold_during_pause_waits_for_running(self):
        await self.pause()
        await self.bot.handle_update(message(4,'/hold 30m'))
        self.assertIsNone(self.db.get_state()['hold_until'])
        self.assertEqual(self.db.get_state()['pending_hold_seconds'],1800)
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('resume')),1)
        self.assertIsNone(self.db.get_state()['hold_until'])
        self.client.state = 'running'
        await self.scheduler._tick()
        hold = parse_iso(self.db.get_state()['hold_until'])
        self.assertAlmostEqual((hold-datetime.now(timezone.utc)).total_seconds(),1800,delta=2)

    async def test_resume_coalesces_and_survives_restart(self):
        await self.pause()
        await self.bot.handle_update(message(4,'/resume'))
        key = self.db.get_state()['resume_key']
        await self.scheduler._tick()
        restarted = LeaseScheduler(self.cfg,Database(self.cfg.db_path),self.client)
        await restarted.initialize()
        await asyncio.gather(restarted.request_resume(),restarted.request_resume())
        await restarted._tick()
        self.assertEqual(self.calls('resume'),[key])
        self.assertIn('op_1',self.calls('operation'))

    async def test_cooldown_retry_uses_same_key_and_never_start(self):
        await self.pause()
        self.client.resume_error = InstaBoxError('cooldown',409,cooldown_until=iso(datetime.now(timezone.utc)+timedelta(minutes=10)))
        await self.bot.handle_update(message(4,'/resume'))
        await self.scheduler._tick()
        key = self.db.get_state()['resume_key']
        await self.scheduler._tick()
        self.assertEqual(self.calls('resume'),[key])
        self.client.resume_error = None
        self.db.update_state(cooldown_until=iso(datetime.now(timezone.utc)-timedelta(seconds=1)),resume_retry_at=iso(datetime.now(timezone.utc)-timedelta(seconds=1)))
        await self.scheduler._tick()
        self.assertEqual(self.calls('resume'),[key,key])
        self.assertEqual(self.calls('start'),[])

    async def test_platform_cooldown_is_observed_before_post(self):
        await self.pause()
        self.client.cooldown = iso(datetime.now(timezone.utc)+timedelta(minutes=10))
        await self.bot.handle_update(message(4,'/resume'))
        await self.scheduler._tick()
        self.assertEqual(self.calls('resume'),[])
        self.assertEqual(self.db.get_state()['cooldown_until'],self.client.cooldown)

    async def test_permission_error_stops_automatic_retry(self):
        await self.pause()
        self.client.resume_error = InstaBoxError('forbidden',403)
        await self.bot.handle_update(message(4,'/resume'))
        await self.scheduler._tick()
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('resume')),1)
        self.assertEqual(self.db.get_state()['resume_phase'],'failed')

    async def test_stopped_never_resume_or_start_by_default(self):
        self.client.state = 'stopped'
        await self.scheduler._tick()
        await self.bot.handle_update(message(4,'/resume'))
        await self.scheduler._tick()
        self.assertEqual(self.calls('resume'),[])
        self.assertEqual(self.calls('start'),[])

    async def test_running_machine_can_explicitly_retry_after_permission_fix(self):
        self.db.update_state(api_blocked=1,last_error='HTTP 403')
        await self.bot.handle_update(message(12,'/resume'))
        self.assertEqual(self.db.get_state()['api_blocked'],0)
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('put')),1)

    async def test_lease_rate_limit_stops_api_io_until_retry_after(self):
        async def limited(*args):
            self.client.calls.append(('put',args))
            raise InstaBoxError('rate limited',429,retry_after=600)
        self.client.put_lease=limited
        await self.bot.handle_update(message(12,'/hold 60m'))
        await self.scheduler._tick()
        count=len(self.client.calls)
        await self.scheduler._tick()
        self.assertEqual(len(self.client.calls),count)
        self.db.update_state(api_retry_at=iso(datetime.now(timezone.utc)-timedelta(seconds=1)))
        await self.scheduler._tick()
        self.assertEqual(len(self.calls('put')),2)

    async def test_hold_cap_does_not_slide(self):
        origin = datetime.now(timezone.utc)
        self.db.update_state(session_started_at=iso(origin-timedelta(hours=5)))
        self.scheduler.trigger_activity(21600)
        hold = parse_iso(self.db.get_state()['hold_until'])
        self.assertAlmostEqual((hold-origin).total_seconds(),3600,delta=2)
        self.db.record_heartbeat(self.cfg.agent_id,'1','busy','task',10)
        await self.scheduler._tick()
        self.assertLessEqual(parse_iso(self.db.get_state()['task_hold_until']),origin+timedelta(hours=1))

    async def test_fresh_database_uses_configured_mode_and_persists_changes(self):
        cfg = ControllerConfig(machine_id='m_other',keep_mode='manual',db_path=self.tmp.name+'/fresh.db')
        db = Database(cfg.db_path)
        scheduler = LeaseScheduler(cfg,db,self.client)
        await scheduler.initialize()
        self.assertEqual(scheduler.get_mode(),'manual')
        scheduler.set_mode('scheduled')
        await scheduler.initialize()
        self.assertEqual(scheduler.get_mode(),'scheduled')

    async def test_rebinding_volume_to_other_machine_is_rejected(self):
        self.cfg.machine_id = 'm_other'
        with self.assertRaises(ValueError):
            await self.scheduler.initialize()

    async def test_schedule_uses_timezone_weekdays_and_previous_day(self):
        self.scheduler.configure_schedule('09:00','18:00','Asia/Shanghai',[0,1,2,3,4])
        self.assertFalse(self.scheduler._is_in_schedule(datetime(2026,11,5,12,tzinfo=timezone.utc)))
        self.assertTrue(self.scheduler._is_in_schedule(datetime(2026,11,5,4,tzinfo=timezone.utc)))
        self.scheduler.configure_schedule('22:00','02:00','UTC',[4])
        self.assertTrue(self.scheduler._is_in_schedule(datetime(2026,11,7,1,tzinfo=timezone.utc)))
        self.assertFalse(self.scheduler._is_in_schedule(datetime(2026,11,8,1,tzinfo=timezone.utc)))

    async def test_schedule_command_is_control_and_explicitly_enabled(self):
        await self.bot.handle_update(message(4,'/schedule Asia/Shanghai 09:00-18:00 0,1,2,3,4 wake=0'))
        self.assertEqual(self.db.pull_pending_updates(),[])
        self.assertEqual(self.scheduler.get_mode(),'on_demand')
        await self.bot.handle_update(message(5,'/schedule on'))
        self.assertEqual(self.scheduler.get_mode(),'scheduled')
        self.assertFalse(self.scheduler.schedule()['wake'])

    async def test_ready_requires_recent_heartbeat_after_resume(self):
        self.db.record_heartbeat(self.cfg.agent_id,'1','ready')
        await self.pause()
        await self.bot.handle_update(message(4,'/resume'))
        await self.scheduler._tick()
        self.client.state='running'
        await self.scheduler._tick()
        self.assertFalse(self.scheduler.agent_ready())
        self.db.record_heartbeat(self.cfg.agent_id,'1','ready')
        await self.scheduler._tick()
        self.assertTrue(self.scheduler.agent_ready())
        self.assertEqual(self.db.get_state()['resume_phase'],'ready')

    async def test_priority_lane_works_with_full_ordinary_capacity(self):
        self.db.ingest_batch([message(1),message(2,'/cancel')])
        self.assertEqual(self.db.next_received()['update_id'],2)
        await self.bot.handle_update(message(2,'/cancel'))
        self.assertEqual(self.db.pull_pending_updates(limit=0)[0]['update_id'],2)

    async def test_auth_fails_closed_and_fixed_identity_is_checked(self):
        api = AgentApiServer(self.cfg,self.db,self.scheduler)
        self.assertEqual((await api._route_request('POST','/agent/pull',{},{}))[0],401)
        self.assertEqual((await api._route_request('POST','/agent/pull',{'x-agent-secret':'s'*32},{'agent_id':'stranger','machine_id':'m_test'}))[0],403)
        self.cfg.agent_secret=''
        self.assertFalse(api._check_auth({}))
        self.cfg.tg_allowed_users=[]
        self.assertFalse(self.bot._is_allowed(7))

    async def test_local_inbox_commit_precedes_gateway_ack_and_deduplicates(self):
        inbox = GatewayInbox(self.tmp.name+'/inbox.db')
        client = GatewayClient('http://127.0.0.1:1234','secret','m_test')
        client.pull=lambda *args:[message(8)]
        def ack(ids):
            self.assertEqual(inbox.next_update()[0]['update_id'],8)
            raise OSError('ack lost')
        client.ack=ack
        with self.assertRaises(OSError):
            client.receive_into(inbox)
        client.ack=lambda ids:True
        client.receive_into(inbox)
        self.assertEqual(inbox.next_update()[0]['update_id'],8)
        inbox.complete(8)
        client.receive_into(inbox)
        self.assertIsNone(inbox.next_update())

    async def test_late_text_and_changed_task_are_rejected(self):
        saved=dict(prompt_token='old',prompt_message_id=1,prompt_created_at=10,task_id='old',task_started_at=10)
        current=dict(prompt_token='new',prompt_message_id=2,prompt_created_at=20,task_id='new',task_started_at=20)
        self.assertFalse(interaction_is_current(message(3,date=15),saved,current,allowed_users=[7]))
        self.assertFalse(interaction_is_current(message(4,'/cancel',date=15),saved,current,allowed_users=[7]))

    async def test_supervisor_treats_component_exit_as_failure(self):
        task=asyncio.create_task(asyncio.sleep(0),name='failed_component')
        with self.assertRaises(RuntimeError):
            await supervise([task],asyncio.Event())


class ConfigurationAndHttpTests(unittest.TestCase):
    def test_secrets_from_files_and_required_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=tmp+'/secret'
            with open(path,'w') as file:
                file.write('s'*32)
            with patch.dict(os.environ,{'AGENT_API_SECRET_FILE':path,'INSTABOX_API_KEY':'dummy','INSTABOX_MACHINE_ID':'m_fixed'},clear=True):
                cfg=ControllerConfig.from_env().validate(required=True)
                self.assertEqual(cfg.agent_secret,'s'*32)
            with self.assertRaises(ValueError):
                ControllerConfig().validate(required=True)
            with self.assertRaises(ValueError):
                ControllerConfig(tg_token='dummy',api_key='dummy',machine_id='m_fixed',agent_secret='s'*32).validate(required=True)

    def test_invalid_config_is_rejected_without_creating_db_directory(self):
        with patch.dict(os.environ,{'INSTABOX_RENEW_EVERY':'0'},clear=True):
            with self.assertRaises(ValueError):
                ControllerConfig.from_env()
        with self.assertRaises(ValueError):
            ControllerConfig(schedule_start='99:00').validate()

    def test_string_errors_and_retry_after_dates_are_typed_and_redacted(self):
        headers=Message()
        headers['Retry-After']='Fri, 01 Jan 2100 00:00:00 GMT'
        error=urllib.error.HTTPError('http://fake',409,'Conflict',headers,io.BytesIO(b'{"error":"machine_cooldown","retryAfterSeconds":300,"echo":"secret-value"}'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(InstaBoxError) as captured:
                InstaBoxClient('http://fake','secret-value')._request('POST','/v1/machines/x/resume',{})
        self.assertEqual(captured.exception.code,'machine_cooldown')
        self.assertGreater(captured.exception.retry_after,300)
        self.assertNotIn('secret-value',str(captured.exception))

    def test_public_agent_http_is_rejected(self):
        with self.assertRaises(ValueError):
            GatewayClient('http://8.8.8.8:8080','secret','m_test')
