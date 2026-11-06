"""One durable policy and lifecycle state machine; only ticks perform platform I/O."""
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from controller.config import validate_schedule, schedule_timezone
from controller.db import now_iso
from controller.instabox import InstaBoxError

logger = logging.getLogger('scheduler')
ACTIVE_PHASES = ('requested', 'cooldown', 'retry', 'resuming', 'awaiting_agent')
SLEEP_PHASES = ('requested', 'pausing', 'retry')


def parse_iso(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError, AttributeError):
        return None


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


class LeaseScheduler:
    def __init__(self, config, db, client):
        self.cfg, self.db, self.client = config, db, client
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self._running = False
        self.resolved_machine_id = ''
        self.last_tick = None

    async def initialize(self):
        if not self.cfg.machine_id or self.cfg.machine_id == 'all':
            raise ValueError('Configure a fixed INSTABOX_MACHINE_ID')
        self.resolved_machine_id = await self.client.resolve_machine_id(self.cfg.machine_id)
        machine = await self.client.get_machine(self.resolved_machine_id)
        with self.db.transaction():
            st = self.db.get_state()
            if st['machine_id'] and st['machine_id'] != self.resolved_machine_id:
                raise ValueError('Data volume belongs to another machine; use a separate volume')
            if st['machine_id'] and st['lease_name'] != self.cfg.lease_name:
                raise ValueError('Lease ownership changed; release old lease before changing volume/config')
            self.db.update_state(machine_id=self.resolved_machine_id, machine_name=machine.get('name', self.cfg.machine_id),
                                 lease_name=self.cfg.lease_name, keep_mode=st['keep_mode'] or self.cfg.keep_mode)
            if not st['schedule_json']:
                self.db.update_state(schedule_json=json.dumps(self.default_schedule()))
            self._observe(machine)

    @asynccontextmanager
    async def transaction(self):
        # Commands commit their policy, disposition and reply in one transaction.
        async with self.lock:
            with self.db.transaction():
                yield
            self.changed.set()

    def default_schedule(self):
        return dict(start=self.cfg.schedule_start, end=self.cfg.schedule_end, timezone=self.cfg.timezone,
                    days=list(self.cfg.schedule_days), wake=self.cfg.schedule_wake)

    def schedule(self):
        value = self.db.get_state()['schedule_json']
        return json.loads(value) if value else self.default_schedule()

    def configure_schedule(self, start, end, tz, days, wake=False):
        validate_schedule(start, end, tz, days)
        value = dict(start=start, end=end, timezone=tz, days=list(days), wake=bool(wake))
        self.db.update_state(schedule_json=json.dumps(value), schedule_active=0)
        return value

    def set_mode(self, mode):
        mode = 'on_demand' if mode == 'auto' else mode
        if mode not in ('manual', 'on_demand', 'scheduled'):
            raise ValueError('支持 auto / manual / scheduled')
        self.db.update_state(keep_mode=mode)
        if mode != 'on_demand':
            self.db.update_state(task_hold_until=None)
        return mode

    def get_mode(self):
        return self.db.get_state()['keep_mode'] or self.cfg.keep_mode

    def trigger_activity(self, duration_seconds=None, explicit=False):
        """No API I/O; callers hold transaction(). Manual heartbeats never call this."""
        duration = duration_seconds if duration_seconds is not None else self.cfg.hold_window_seconds
        if type(duration) is not int or duration <= 0:
            raise ValueError('保活时长必须为正整数')
        if duration > self.cfg.max_hold_seconds:
            raise ValueError(f'保活最多 {self.cfg.max_hold_seconds // 60} 分钟，不能超过 8 小时')
        st = self.db.get_state()
        if st['sleep_phase'] in SLEEP_PHASES:
            raise ValueError('休眠正在处理，完成后再使用 /resume 或 /hold')
        if not explicit and st['keep_mode'] != 'on_demand':
            return None
        now = datetime.now(timezone.utc)
        if st['vm_state'] != 'running':
            pending = max(st['pending_hold_seconds'] or 0, duration)
            self.db.update_state(pending_hold_seconds=pending,
                                 pending_hold_until=iso(now + timedelta(seconds=self.cfg.wake_request_seconds)))
            return None
        start = parse_iso(st['session_started_at']) or now
        current = parse_iso(st['hold_until']) or now
        deadline = min(max(current, now + timedelta(seconds=duration)), start + timedelta(seconds=self.cfg.max_hold_seconds))
        self.db.update_state(session_started_at=iso(start), hold_until=iso(deadline))
        return deadline

    def stage_resume(self, reason='user', explicit=True, chat_id=None):
        now, st = datetime.now(timezone.utc), self.db.get_state()
        if st['sleep_phase'] in SLEEP_PHASES:
            return False, '休眠正在处理，完成后再使用 /resume 或 /hold。'
        if st['api_blocked'] and not explicit:
            return False, '机器 API 权限异常，需修正凭据并显式重试。'
        if explicit and st['api_blocked']:
            self.db.update_state(api_blocked=0, last_error=None)
            st = self.db.get_state()
        if st['vm_state'] == 'running':
            return True, '机器运行中；Bridge 已就绪。' if self.agent_ready() else '机器运行中，Bridge 尚未就绪。'
        # Definite manual pause is rejected before staging; unknown is checked again against the API.
        if not explicit and st['vm_state'] in ('paused', 'suspended') and not self._automatic_pause(st):
            return False, '机器为手动暂停或暂停来源不明，请使用 /resume 明确唤醒。'
        if st['resume_phase'] in ACTIVE_PHASES:
            if explicit and not st['resume_explicit']:
                self.db.update_state(resume_explicit=1)
            return True, '恢复请求已保存，重复请求已合并；请查看 /vmstatus。'
        self.db.update_state(resume_key=str(uuid.uuid4()), resume_phase='requested', resume_operation=None,
                             resume_reason=reason, resume_explicit=int(explicit), resume_until=iso(now + timedelta(seconds=self.cfg.wake_request_seconds)),
                             resume_retry_at=None, resume_attempts=0, api_blocked=0 if explicit else st['api_blocked'],
                             agent_valid_after=iso(now), last_error=None)
        if chat_id is not None:
            self.db.set_meta('resume_notify:' + self.db.get_state()['resume_key'], str(chat_id))
        return True, '已保存恢复请求；运行和 Bridge 就绪后会通知。'

    async def request_resume(self, reason='user'):
        async with self.transaction():
            return self.stage_resume(reason, explicit=reason not in ('auto_chat_wake', 'schedule'))

    def stage_release(self):
        self.db.update_state(keep_mode='manual', hold_until=None, pending_hold_seconds=0, pending_hold_until=None,
                             task_hold_until=None, resume_phase='idle', resume_key=None,
                             resume_operation=None, resume_until=None, resume_retry_at=None, release_requested=1)
        return True, '已停止本服务续租并请求释放自身租约；不会直接暂停机器。'

    async def release_lease(self):
        async with self.transaction():
            return self.stage_release()

    def stage_sleep(self, chat_id=None):
        st = self.db.get_state()
        if st['sleep_phase'] in SLEEP_PHASES:
            return True, '休眠请求已保存，重复请求已合并。'
        if st['resume_phase'] in ACTIVE_PHASES:
            return False, '机器正在恢复，请待恢复完成后再使用 /sleep。'
        self.stage_release()
        key = str(uuid.uuid4())
        self.db.update_state(sleep_key=key, sleep_phase='requested', sleep_operation=None,
                             sleep_retry_at=None, sleep_until=iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
                             api_blocked=0, last_error=None)
        if chat_id is not None:
            self.db.set_meta('sleep_notify:' + key, str(chat_id))
        return True, '已请求休眠；保活已结束，运行中的进程和任务将随内存暂停。之后使用 /resume 或 /hold 唤醒。'

    async def _sleep_tick(self):
        st, now = self.db.get_state(), datetime.now(timezone.utc)
        if st['sleep_phase'] not in SLEEP_PHASES:
            return False
        if st['vm_state'] in ('paused', 'suspended', 'stopped'):
            self.db.update_state(sleep_phase='done', agent_valid_after=iso(now))
            chat = self.db.get_meta('sleep_notify:' + st['sleep_key'])
            if chat:
                self.db.add_reply('sleep:' + st['sleep_key'], int(chat),
                                  '机器已休眠。使用 /resume 唤醒，或 /hold 2h 唤醒并保活。')
            return True
        if parse_iso(st['sleep_until']) and now >= parse_iso(st['sleep_until']):
            self.db.update_state(sleep_phase='failed', last_error='休眠请求超时，请查看平台状态。')
            return True
        if parse_iso(st['sleep_retry_at']) and now < parse_iso(st['sleep_retry_at']):
            return True
        try:
            if st['sleep_operation']:
                result = await self.client.get_operation(st['sleep_operation'])
                if (result.get('state') or result.get('status')) in ('failed', 'error', 'cancelled'):
                    self.db.update_state(sleep_phase='failed', last_error='平台休眠操作失败。')
                return True
            if st['vm_state'] != 'running':
                return True
            result = await self.client.suspend_machine(self.resolved_machine_id, st['sleep_key'])
            operation = result.get('operation', result)
            op_id = operation.get('id') or result.get('operationId')
            self.db.update_state(sleep_phase='pausing', sleep_operation=op_id,
                                 sleep_retry_at=None if op_id else iso(now + timedelta(seconds=30)))
        except InstaBoxError as error:
            if error.code == 'operation_conflict' and error.body.get('operationId'):
                self.db.update_state(sleep_phase='pausing', sleep_operation=error.body['operationId'])
            elif error.status_code in (400, 401, 403, 404):
                self.db.update_state(sleep_phase='failed', api_blocked=int(error.status_code in (401, 403)),
                                     last_error=f'休眠失败：HTTP {error.status_code}')
            else:
                self.db.update_state(sleep_phase='retry', sleep_retry_at=iso(now + timedelta(seconds=max(10, error.retry_after or 30))),
                                     last_error='休眠请求暂时失败，将重试同一个请求。')
        return True

    def _automatic_pause(self, st):
        reason = (st.get('suspend_reason') or '').lower()
        return reason not in ('user', 'manual') and bool(st.get('auto_suspended_at'))

    def _observe(self, machine):
        old = self.db.get_state()
        state = machine.get('state', 'unknown')
        changes = dict(vm_state=state, auto_suspended_at=machine.get('autoSuspendedAt'),
                       suspend_reason=machine.get('suspendReason') or machine.get('autoSuspendReason') or machine.get('suspensionReason'))
        cooldown = parse_iso(machine.get('cooldownUntil'))
        if cooldown:
            changes['cooldown_until'] = iso(cooldown)
        if old['vm_state'] == 'running' and state in ('paused', 'suspended', 'stopped'):
            changes.update(session_started_at=None, task_started_at=None, task_hold_until=None,
                           agent_valid_after=now_iso())
        self.db.update_state(**changes)

    def agent_ready(self):
        st = self.db.get_state()
        agent = self.db.get_latest_agent_status(self.cfg.agent_id)
        seen = parse_iso(agent['last_seen_at']) if agent else None
        after = parse_iso(st['agent_valid_after'])
        age = (datetime.now(timezone.utc) - seen).total_seconds() if seen else float('inf')
        return bool(st['vm_state'] == 'running' and 0 <= age <= self.cfg.agent_fresh_seconds
                    and (not after or seen >= after) and agent['codex_status'] in ('ready', 'busy'))

    def _is_in_schedule(self, now):
        schedule = self.schedule()
        local = now.astimezone(schedule_timezone(schedule['timezone']))
        value = local.strftime('%H:%M')
        start, end = schedule['start'], schedule['end']
        if start < end:
            return local.weekday() in schedule['days'] and start <= value < end
        # The after-midnight section belongs to the preceding day's window.
        day = local.weekday() if value >= start else (local.weekday() - 1) % 7
        return day in schedule['days'] and (value >= start or value < end)

    def _error(self, error, resume=False):
        now, st = datetime.now(timezone.utc), self.db.get_state()
        self.db.update_state(last_error=str(error))
        deadline = parse_iso(error.cooldown_until)
        if error.retry_after is not None:
            retry = now + timedelta(seconds=max(0, error.retry_after))
            deadline = max(deadline, retry) if deadline else retry
        if error.status_code in (401, 403):
            self.db.update_state(api_blocked=1)
            if resume:
                self.db.update_state(resume_phase='failed')
            return
        if error.status_code == 429 or (error.status_code >= 500 and error.retry_after is not None):
            self.db.update_state(api_retry_at=iso(deadline or now + timedelta(seconds=60)))
        if resume:
            if error.status_code in (400, 404, 405, 422) or error.code == 'max_run_disabled':
                self.db.update_state(resume_phase='failed', pending_hold_seconds=0, pending_hold_until=None)
                return
            attempts = (st['resume_attempts'] or 0) + 1
            retry = deadline or now + timedelta(seconds=min(60, 2 ** min(attempts, 6)))
            self.db.update_state(resume_phase='cooldown' if deadline else 'retry', resume_retry_at=iso(retry), resume_attempts=attempts)
        if deadline:
            self.db.update_state(cooldown_until=iso(deadline))

    async def _resume_tick(self):
        now, st = datetime.now(timezone.utc), self.db.get_state()
        if st['resume_phase'] not in ACTIVE_PHASES:
            return
        if parse_iso(st['resume_until']) and now >= parse_iso(st['resume_until']):
            self.db.update_state(resume_phase='expired', pending_hold_seconds=0, pending_hold_until=None)
            return
        if st['vm_state'] == 'running':
            self.db.update_state(resume_phase='awaiting_agent')
            if self.cfg.gateway_mode == 'control_only' or self.agent_ready():
                self.db.update_state(resume_phase='ready', last_error=None)
                chat = self.db.get_meta('resume_notify:' + st['resume_key'])
                if chat:
                    self.db.add_reply('ready:' + st['resume_key'], int(chat),
                        '机器已恢复，可回对话 bot 继续。' if self.cfg.gateway_mode == 'control_only'
                        else '机器已恢复，Bridge/Codex 已就绪，可继续对话。')
            return
        if not st['resume_explicit'] and not self._automatic_pause(st):
            self.db.update_state(resume_phase='needs_explicit', last_error='手动暂停或来源不明，需要 /resume')
            return
        deadline = max(filter(None, [parse_iso(st['cooldown_until']), parse_iso(st['resume_retry_at'])]), default=None)
        if deadline and now < deadline:
            self.db.update_state(resume_phase='cooldown')
            return
        if st['resume_operation']:
            try:
                operation = await self.client.get_operation(st['resume_operation'])
                status = operation.get('state') or operation.get('status')
                if status in ('failed', 'error', 'cancelled'):
                    self.db.update_state(resume_phase='failed', last_error='恢复操作失败，请检查平台操作记录。')
                # Keep tracking until the machine itself is running; never re-POST an accepted operation.
                return
            except InstaBoxError as error:
                self._error(error, resume=True)
                return
        if st['vm_state'] in ('paused', 'suspended'):
            action = self.client.resume_machine
        elif st['vm_state'] == 'stopped' and self.cfg.auto_start and st['resume_explicit']:
            action = self.client.start_machine
        elif st['vm_state'] == 'stopped':
            self.db.update_state(resume_phase='failed', last_error='Stopped：自动 Start 默认关闭。')
            return
        else:
            return
        try:
            result = await action(self.resolved_machine_id, idempotency_key=st['resume_key'])
            op = result.get('operation', result)
            self.db.update_state(resume_phase='resuming', resume_operation=op.get('id') or result.get('operationId'),
                                 resume_retry_at=None, last_error=None)
            # Without an operation ID, wait before retrying the same persisted idempotency key.
            if not self.db.get_state()['resume_operation']:
                self.db.update_state(resume_retry_at=iso(now + timedelta(seconds=30)))
        except InstaBoxError as error:
            op = error.body.get('operationId')
            if error.code == 'operation_conflict' and op:
                self.db.update_state(resume_operation=op, resume_phase='resuming')
            else:
                self._error(error, resume=True)

    def _activate_and_decide(self):
        now, st = datetime.now(timezone.utc), self.db.get_state()
        pending_until = parse_iso(st['pending_hold_until'])
        if pending_until and now >= pending_until:
            self.db.update_state(pending_hold_seconds=0, pending_hold_until=None)
        elif st['vm_state'] == 'running' and st['pending_hold_seconds']:
            self.trigger_activity(st['pending_hold_seconds'], explicit=True)
            self.db.update_state(pending_hold_seconds=0, pending_hold_until=None)
        st = self.db.get_state()
        mode = st['keep_mode']
        if mode == 'scheduled':
            active = self._is_in_schedule(now)
            start = parse_iso(st['session_started_at'])
            if active and not start and st['vm_state'] == 'running':
                start = now
                self.db.update_state(session_started_at=iso(start))
            if start and now >= start + timedelta(seconds=self.cfg.max_hold_seconds):
                active = False
            if active and not st['schedule_active'] and self.schedule()['wake']:
                self.stage_resume('schedule', explicit=False)
            self.db.update_state(schedule_active=int(active))
            if not active and st['resume_reason'] == 'schedule' and st['resume_phase'] in ACTIVE_PHASES:
                self.db.update_state(resume_phase='expired')
            return active
        if mode == 'on_demand':
            agent = self.db.get_latest_agent_status(self.cfg.agent_id)
            if self.agent_ready() and agent['current_task']:
                start = parse_iso(st['session_started_at']) or now
                task_start = parse_iso(st['task_started_at']) if st['task_id'] == agent['current_task'] else now
                cap = min(start, task_start or now) + timedelta(seconds=self.cfg.max_hold_seconds)
                self.db.update_state(session_started_at=iso(start), task_id=agent['current_task'], task_started_at=iso(task_start or now),
                                     task_hold_until=iso(min(now + timedelta(seconds=600), cap)))
            elif agent and not agent['current_task']:
                self.db.update_state(task_id=None, task_started_at=None, task_hold_until=None)
        st = self.db.get_state()
        until = parse_iso(st['hold_until'])
        task_until = parse_iso(st['task_hold_until']) if mode == 'on_demand' else None
        return bool((until and now < until) or (task_until and now < task_until))

    async def _tick(self):
        async with self.lock:
            self.last_tick = datetime.now(timezone.utc)
            st = self.db.get_state()
            if st['api_blocked']:
                return
            retry_at = parse_iso(st['api_retry_at'])
            if retry_at and self.last_tick < retry_at:
                return
            if retry_at:
                self.db.update_state(api_retry_at=None)
            if st['release_requested']:
                try:
                    if not await self.client.delete_lease(self.resolved_machine_id, self.cfg.lease_name):
                        raise InstaBoxError('Lease deletion was not confirmed')
                    self.db.update_state(release_requested=0, lease_expires_at=None, last_renew_at=None, last_error=None)
                except InstaBoxError as error:
                    self._error(error)
                return
            try:
                self._observe(await self.client.get_machine(self.resolved_machine_id))
            except InstaBoxError as error:
                self._error(error, resume=st['resume_phase'] in ACTIVE_PHASES)
                return
            need_hold = self._activate_and_decide()
            if await self._sleep_tick():
                return
            await self._resume_tick()
            st, now = self.db.get_state(), datetime.now(timezone.utc)
            if st['api_blocked']:
                return
            if need_hold and st['vm_state'] == 'running':
                renewed = parse_iso(st['last_renew_at'])
                if renewed and (now - renewed).total_seconds() < self.cfg.renew_interval and parse_iso(st['lease_expires_at']) and parse_iso(st['lease_expires_at']) > now:
                    return
                try:
                    result = await self.client.put_lease(self.resolved_machine_id, self.cfg.lease_name, self.cfg.lease_ttl)
                    self.db.update_state(last_renew_at=iso(now), lease_expires_at=result.get('expiresAt') or iso(now + timedelta(seconds=self.cfg.lease_ttl)), last_error=None)
                except InstaBoxError as error:
                    self._error(error)
                    # Refresh true state, but never turn a lease failure into Start/Resume.
                    if parse_iso(self.db.get_state()['api_retry_at']):
                        return
                    try:
                        self._observe(await self.client.get_machine(self.resolved_machine_id))
                    except InstaBoxError as read_error:
                        self._error(read_error)
            elif not need_hold and st['lease_expires_at']:
                self.db.update_state(release_requested=1)

    async def run_loop(self):
        self._running = True
        while self._running:
            self.changed.clear()
            await self._tick()
            try:
                await asyncio.wait_for(self.changed.wait(), self.cfg.tick_seconds)
            except TimeoutError:
                pass

    async def stop(self):
        self._running = False
        self.changed.set()
        async with self.lock:
            if self.resolved_machine_id:
                try:
                    await self.client.delete_lease(self.resolved_machine_id, self.cfg.lease_name)
                    self.db.update_state(lease_expires_at=None, last_renew_at=None)
                except InstaBoxError as error:
                    self.db.update_state(last_error=str(error), release_requested=1)
