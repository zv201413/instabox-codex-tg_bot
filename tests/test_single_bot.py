import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from controller.config import ControllerConfig
from controller.db import Database
from controller.scheduler import LeaseScheduler, iso, parse_iso
from controller.telegram_bot import TelegramBot
from test_regressions import FakeClient, message


class SleepClient(FakeClient):
    async def suspend_machine(self, machine_id, idempotency_key):
        self.calls.append(('sleep', idempotency_key))
        return {'id':'op_sleep', 'state':'queued'}


class SingleBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ControllerConfig(machine_id='m_test', api_key='dummy', agent_secret='s'*32,
                                    max_hold_seconds=28800, tg_allowed_users=[7], db_path=self.tmp.name+'/db')
        self.db = Database(self.cfg.db_path)
        self.client = SleepClient()
        self.scheduler = LeaseScheduler(self.cfg, self.db, self.client)
        await self.scheduler.initialize()
        self.bot = TelegramBot(self.cfg, self.db, self.scheduler)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_eight_hour_hold_and_larger_request_rejected(self):
        await self.bot.handle_update(message(1, '/hold 8h'))
        first = self.db.get_state()['hold_until']
        self.assertAlmostEqual((parse_iso(first)-datetime.now(timezone.utc)).total_seconds(), 28800, delta=2)
        await self.bot.handle_update(message(2, '/hold 9h'))
        self.assertEqual(self.db.get_state()['hold_until'], first)
        with self.assertRaises(ValueError):
            ControllerConfig(max_hold_seconds=28801).validate()

    async def test_sleep_releases_lease_before_suspending_and_keeps_manual(self):
        await self.bot.handle_update(message(1, '/hold 2h'))
        await self.scheduler._tick()
        await self.bot.handle_update(message(2, '/sleep'))
        await self.scheduler._tick()
        await self.scheduler._tick()
        kinds = [kind for kind, _ in self.client.calls]
        self.assertLess(kinds.index('delete'), kinds.index('sleep'))
        self.assertNotIn('start', kinds)
        self.assertEqual(self.db.get_state()['keep_mode'], 'manual')
        self.assertIsNone(self.db.get_state()['hold_until'])

    async def test_repeated_sleep_and_controller_restart_keep_one_operation(self):
        await self.bot.handle_update(message(1, '/sleep'))
        key = self.db.get_state()['sleep_key']
        await self.scheduler._tick()
        await self.scheduler._tick()
        await self.bot.handle_update(message(2, '/sleep'))
        restarted = LeaseScheduler(self.cfg, Database(self.cfg.db_path), self.client)
        await restarted.initialize()
        await restarted._tick()
        self.assertEqual([value for kind,value in self.client.calls if kind=='sleep'], [key])
        self.assertIn(('operation','op_sleep'), self.client.calls)

    async def test_sleep_finishes_then_explicit_resume_is_available(self):
        await self.bot.handle_update(message(1, '/sleep'))
        await self.scheduler._tick()
        await self.scheduler._tick()
        self.client.state='suspended'
        await self.scheduler._tick()
        self.assertEqual(self.db.get_state()['sleep_phase'], 'done')
        await self.bot.handle_update(message(2, 'ordinary message'))
        await self.scheduler._tick()
        self.assertNotIn('resume', [kind for kind,_ in self.client.calls])
        await self.bot.handle_update(message(3, '/resume'))
        await self.scheduler._tick()
        self.assertIn('resume', [kind for kind,_ in self.client.calls])

    async def test_wake_during_sleep_does_not_cancel_accepted_pause(self):
        await self.bot.handle_update(message(1, '/sleep'))
        key=self.db.get_state()['sleep_key']
        await self.bot.handle_update(message(2, '/hold 1h'))
        await self.bot.handle_update(message(3, '/resume'))
        self.assertEqual(self.db.get_state()['sleep_key'], key)
        self.assertEqual(self.db.get_state()['sleep_phase'], 'requested')
        self.assertIsNone(self.db.get_state()['hold_until'])

    async def test_sleep_during_resume_is_rejected(self):
        self.client.state='suspended'
        await self.scheduler._tick()
        await self.bot.handle_update(message(1, '/resume'))
        key=self.db.get_state()['resume_key']
        await self.bot.handle_update(message(2, '/sleep'))
        self.assertEqual(self.db.get_state()['resume_key'], key)
        self.assertEqual(self.db.get_state()['sleep_phase'], 'idle')

    async def test_release_does_not_reset_continuous_hold_cap(self):
        start=datetime.now(timezone.utc)-timedelta(hours=7)
        self.db.update_state(session_started_at=iso(start))
        await self.bot.handle_update(message(1, '/release'))
        await self.scheduler._tick()
        await self.bot.handle_update(message(2, '/hold 8h'))
        self.assertLessEqual(parse_iso(self.db.get_state()['hold_until']), start+timedelta(hours=8))

    async def test_schedule_is_also_capped_at_eight_hours(self):
        now=datetime.now(timezone.utc)
        self.scheduler.configure_schedule('00:00','23:59','UTC',list(range(7)))
        self.scheduler.set_mode('scheduled')
        self.db.update_state(session_started_at=iso(now-timedelta(hours=9)))
        self.assertFalse(self.scheduler._activate_and_decide())

    async def test_control_only_reports_machine_recovery_without_fake_agent_ready(self):
        self.cfg.gateway_mode='control_only'
        self.client.state='suspended'
        await self.scheduler._tick()
        await self.bot.handle_update(message(1, '/resume'))
        await self.scheduler._tick()
        self.client.state='running'
        await self.scheduler._tick()
        self.assertEqual(self.db.get_state()['resume_phase'], 'ready')
        self.assertFalse(self.scheduler.agent_ready())

    async def test_group_machine_commands_never_control_vm(self):
        event=message(1,'/sleep')
        event['message']['chat']={'id':-100,'type':'group'}
        await self.bot.handle_update(event)
        self.assertEqual(self.db.get_state()['sleep_phase'], 'idle')


if __name__=='__main__':
    unittest.main()
