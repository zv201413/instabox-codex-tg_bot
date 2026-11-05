"""Telegram intake commits first. Dispatch, lifecycle I/O and replies are independent."""
import asyncio
import html
import json
import logging
import time
import urllib.request
import urllib.error

from controller.scheduler import parse_iso

logger = logging.getLogger('tg_bot')


class TelegramBot:
    def __init__(self, config, db, scheduler):
        self.cfg, self.db, self.scheduler = config, db, scheduler
        self.base_url = f'https://api.telegram.org/bot{config.tg_token}'
        self._running = True
        self.last_poll = time.monotonic()
        self.last_success = time.monotonic()
        self.last_dispatch = time.monotonic()
        self.last_outbox = time.monotonic()
        self.last_callback = time.monotonic()

    def _call_tg_api(self, method, payload=None, timeout=35):
        request = urllib.request.Request(self.base_url + '/' + method,
            data=json.dumps(payload or {}).encode(), headers={'Content-Type':'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            return {'ok':False,'error_code':error.code}
        except Exception:
            # urllib errors can contain the token-bearing URL; do not stringify them.
            logger.warning('Telegram request failed: %s', method)
            return {'ok':False}

    async def send_message(self, chat_id, text, reply_markup=None):
        return await asyncio.to_thread(self._call_tg_api, 'sendMessage',
            dict(chat_id=chat_id, text=text, reply_markup=reply_markup or self._get_control_keyboard()))

    async def answer_callback_query(self, query_id, text=None):
        return await asyncio.to_thread(self._call_tg_api, 'answerCallbackQuery',
            dict(callback_query_id=query_id, text=text or '已收到'), timeout=5)

    def _get_control_keyboard(self):
        return {'inline_keyboard':[
            [{'text':'唤醒','callback_data':'gw:resume'}, {'text':'保活 1 小时','callback_data':'gw:hold_60m'}],
            [{'text':'结束保活','callback_data':'gw:release'}, {'text':'机器状态','callback_data':'gw:status'}],
            [{'text':'保活设置','callback_data':'gw:settings'}]]}

    def _is_allowed(self, user_id):
        return bool(self.cfg.tg_allowed_users and user_id in self.cfg.tg_allowed_users)

    def _format_status_message(self):
        st, schedule = self.db.get_state(), self.scheduler.schedule()
        agent = '已就绪' if self.scheduler.agent_ready() else '未就绪或心跳已过期'
        return (f"机器：{st['machine_name']} ({st['vm_state']})\nBridge/Codex：{agent}\n"
                f"模式：{st['keep_mode']}\n保活截止：{st['hold_until'] or '无'}\n"
                f"待生效保活：{st['pending_hold_seconds'] or 0} 秒\n租约截止：{st['lease_expires_at'] or '无'}\n"
                f"释放处理中：{'是' if st['release_requested'] else '否'}\n恢复阶段：{st['resume_phase']}\n"
                f"冷却截止：{st['cooldown_until'] or '无'}\n重试时间：{st['resume_retry_at'] or '无'}\n"
                f"API 等待截止：{st['api_retry_at'] or '无'}\n"
                f"计划：{schedule['timezone']} {schedule['start']}-{schedule['end']} days={','.join(map(str,schedule['days']))} wake={int(schedule['wake'])}\n"
                f"待交付：{self.db.get_queue_stats()['pending'] + self.db.get_queue_stats()['delivered']}\n"
                f"最近错误：{st['last_error'] or '无'}")

    async def handle_update(self, update):
        # Public entrypoint also persists first; the poller uses ingest_batch before calling dispatch.
        uid = update['update_id']
        async with self.scheduler.transaction():
            if self.db.is_handled(uid):
                return
            item = update.get('message') or update.get('callback_query', {})
            msg = item.get('message', item)
            text = (item.get('text') or item.get('caption') or '').strip()
            self.db.enqueue_update(uid, msg.get('chat', {}).get('id'), item.get('from', {}).get('id'), msg.get('message_id'),
                                   text, json.dumps(update, ensure_ascii=False), status='received', priority=int('callback_query' in update or text.startswith('/')))
            chat = msg.get('chat', {}).get('id')
            bridge = False
            reply = None
            if not self._is_allowed(item.get('from', {}).get('id')):
                self.db.finish_update(uid)
                return
            cb = update.get('callback_query')
            command, args = '', []
            if cb and cb.get('data', '').startswith('gw:'):
                command = {'resume':'/resume','hold_60m':'/hold','release':'/release','status':'/vmstatus','settings':'/settings'}.get(cb['data'][3:], '/unknown_gateway')
                args = ['60m'] if command == '/hold' else []
            elif not cb and text.startswith('/'):
                parts = text.split()
                command, args = parts[0].split('@')[0].lower(), parts[1:]
            try:
                if command in ('/resume','/wake'):
                    if self.scheduler.get_mode() == 'on_demand':
                        self.scheduler.trigger_activity()
                    _, reply = self.scheduler.stage_resume('user', explicit=True, chat_id=chat)
                elif command == '/hold':
                    value = args[0] if args else '60m'
                    unit = 3600 if value.endswith('h') else 60
                    number = value[:-1] if value.endswith(('h','m')) else value
                    if not number.isdigit() or int(number) <= 0:
                        raise ValueError('用法：/hold 30m 或 /hold 2h（时长大于 0）')
                    deadline = self.scheduler.trigger_activity(int(number) * unit, explicit=True)
                    if deadline is None:
                        _, note = self.scheduler.stage_resume('hold', explicit=True, chat_id=chat)
                        reply = '已保存保活时长，机器确认运行后开始计时。' + note
                    else:
                        reply = '保活截止：' + deadline.isoformat()
                elif command == '/release':
                    _, reply = self.scheduler.stage_release()
                elif command == '/mode':
                    reply = '保活模式：' + (self.scheduler.set_mode(args[0]) if args else self.scheduler.get_mode())
                elif command in ('/vmstatus','/status_gw'):
                    reply = self._format_status_message()
                elif command in ('/schedule','/settings'):
                    if args and args[0] == 'off':
                        self.scheduler.set_mode('manual')
                        reply = '计划已关闭；使用 /release 可同时结束当前显式保活。'
                    elif args and args[0] == 'on':
                        self.scheduler.set_mode('scheduled')
                        reply = '计划已启用。\n' + self._format_status_message()
                    elif args:
                        if len(args) not in (3,4):
                            raise ValueError('用法：/schedule Asia/Shanghai 09:00-18:00 0,1,2,3,4 [wake=0|1]；然后 /schedule on')
                        start, end = args[1].split('-')
                        if len(args) == 4 and args[3] not in ('wake=0','wake=1'):
                            raise ValueError('wake 必须是 0 或 1')
                        self.scheduler.configure_schedule(start,end,args[0],[int(day) for day in args[2].split(',')],len(args)==4 and args[3]=='wake=1')
                        reply = '计划已保存，使用 /schedule on 明确启用。'
                    else:
                        reply = self._format_status_message() + '\n/mode auto 恢复按需保活；/schedule on|off 启停计划。'
                elif command == '/unknown_gateway':
                    reply = '未知的网关按钮，请查看 /settings。'
                elif self.cfg.gateway_mode == 'gateway':
                    bridge = True
                    self.scheduler.trigger_activity()
                    if self.db.get_state()['vm_state'] != 'running' and self.scheduler.get_mode() == 'on_demand':
                        _, note = self.scheduler.stage_resume('auto_chat_wake', explicit=False, chat_id=chat)
                        reply = '消息已保存。' + note
                else:
                    reply = '此 bot 仅管理机器，请使用 /resume、/hold、/release 或 /vmstatus。'
            except (ValueError, TypeError) as error:
                reply = str(error)
            self.db.finish_update(uid, bridge=bridge)
            if reply:
                self.db.add_reply('update:' + str(uid), chat, reply)

    async def run_poll_loop(self):
        self._running = True
        while self._running:
            self.last_poll = time.monotonic()
            payload = {'timeout':self.cfg.tg_poll_timeout, 'allowed_updates':['message','callback_query'],
                       'offset':int(self.db.get_meta('tg_offset','0'))}
            result = await asyncio.to_thread(self._call_tg_api, 'getUpdates', payload, timeout=self.cfg.tg_poll_timeout + 10)
            if not result.get('ok'):
                if result.get('error_code') in (401,409):
                    raise RuntimeError('Telegram token invalid or another intake owns this bot')
                await asyncio.sleep(3)
                continue
            updates = result.get('result', [])
            self.db.ingest_batch(updates)  # failure kills intake, never acknowledges unsaved batch
            self.last_success = time.monotonic()
    async def initialize(self):
        identity = await asyncio.to_thread(self._call_tg_api,'getMe')
        webhook = await asyncio.to_thread(self._call_tg_api,'getWebhookInfo')
        if not identity.get('ok') or not webhook.get('ok') or webhook.get('result',{}).get('url'):
            raise RuntimeError('Telegram token unavailable or existing webhook configured')

    async def run_callback_loop(self):
        while self._running:
            self.last_callback = time.monotonic()
            pending = self.db.next_callback()
            if not pending:
                await asyncio.sleep(0.1)
                continue
            uid, update = pending
            cb = update['callback_query']
            await self.answer_callback_query(cb['id'],'已收到' if self._is_allowed(cb.get('from',{}).get('id')) else '无权限操作')
            # Cosmetic callback acknowledgement must never decide approval or remove the update.
            self.db.mark_callback_answered(uid)

    async def run_dispatch_loop(self):
        while self._running:
            self.last_dispatch = time.monotonic()
            update = self.db.next_received()
            if update:
                await self.handle_update(update)
            else:
                await asyncio.sleep(0.1)

    async def run_outbox_loop(self):
        while self._running:
            self.last_outbox = time.monotonic()
            reply = self.db.next_reply(time.time())
            if not reply:
                await asyncio.sleep(0.2)
                continue
            result = await self.send_message(reply['chat_id'], reply['text'])
            delay = result.get('parameters', {}).get('retry_after') or min(300,2 ** min(reply['attempts'] + 1,8))
            self.db.reply_result(reply['id'], bool(result.get('ok')), time.time() + delay)

    async def stop(self):
        self._running = False
