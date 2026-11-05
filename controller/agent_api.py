"""Bound Bridge API. Public deployments terminate HTTPS at a reverse proxy."""
import asyncio
import hmac
import json
import logging

logger = logging.getLogger('agent_api')


class AgentApiServer:
    def __init__(self, config, db, scheduler, health=None):
        self.cfg, self.db, self.scheduler = config, db, scheduler
        self.health = health
        self.server = None
        self._running = False
        self._slots = asyncio.Semaphore(32)

    def _check_auth(self, headers):
        supplied = headers.get('x-agent-secret') or headers.get('authorization','').removeprefix('Bearer ')
        return bool(self.cfg.agent_secret and supplied and hmac.compare_digest(supplied.encode(), self.cfg.agent_secret.encode()))

    async def _handle_client(self, reader, writer):
        try:
            async with asyncio.timeout(10):
                async with self._slots:
                    await self._respond(reader, writer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _respond(self, reader, writer):
        status, response = 400, {'ok':False,'error':'Invalid HTTP request'}
        try:
            request_line = (await reader.readline()).decode('ascii').strip().split()
            if len(request_line) != 3:
                raise ValueError('Invalid request line')
            method, path, _ = request_line
            headers, total = {}, 0
            while True:
                line = await reader.readline()
                total += len(line)
                if total > 16384:
                    raise ValueError('Headers too large')
                if line in (b'\r\n', b'\n'):
                    break
                if not line:
                    raise ValueError('Incomplete headers')
                key, value = line.decode('ascii').split(':',1)
                key = key.lower().strip()
                if key in headers:
                    raise ValueError('Duplicate header')
                headers[key] = value.strip()
            length = int(headers.get('content-length','0'))
            if not 0 <= length <= 1048576 or 'transfer-encoding' in headers:
                raise ValueError('Invalid body length')
            # Reject unauthenticated requests before reading an attacker-controlled body.
            if path.split('?')[0] not in ('/healthz','/health') and not self._check_auth(headers):
                status, response = 401, {'ok':False,'error':'Unauthorized'}
            else:
                raw = await reader.readexactly(length) if length else b'{}'
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError('Body must be an object')
                status, response = await self._route_request(method,path,headers,body)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            pass
        payload = json.dumps(response,ensure_ascii=False).encode()
        reason = {200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',503:'Service Unavailable'}.get(status,'Error')
        writer.write(f'HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()+payload)
        await writer.drain()

    async def _route_request(self, method, path, headers, body):
        path = path.split('?')[0].rstrip('/') or '/'
        if method == 'GET' and path in ('/healthz','/health'):
            healthy, detail = self.health() if self.health else (self._running, {})
            return (200 if healthy else 503), {'status':'ok' if healthy else 'unhealthy', 'components':detail}
        if not self._check_auth(headers):
            return 401, {'ok':False,'error':'Unauthorized'}
        if body.get('machine_id') != self.scheduler.resolved_machine_id or body.get('agent_id') != self.cfg.agent_id:
            return 403, {'ok':False,'error':'Wrong machine or Bridge identity'}
        try:
            if method == 'POST' and path == '/agent/heartbeat':
                capacity = int(body.get('inbox_capacity',10))
                if not 0 <= capacity <= 100 or body.get('codex_status') not in ('ready','busy','starting','offline'):
                    raise ValueError('Invalid heartbeat')
                task = body.get('current_task')
                if task is not None and (not isinstance(task,str) or len(task) > 512):
                    raise ValueError('Invalid task identity')
                # Record only. The scheduler decides whether heartbeat can extend this policy.
                self.db.record_heartbeat(self.cfg.agent_id,str(body.get('version','')),body['codex_status'],task,capacity)
                self.scheduler.changed.set()
                return 200, {'ok':True}
            if method == 'POST' and path == '/agent/pull':
                if not self.scheduler.agent_ready():
                    return 200, {'ok':True,'updates':[], 'ready':False}
                agent = self.db.get_latest_agent_status(self.cfg.agent_id)
                limit, controls = int(body.get('limit',10)), int(body.get('control_limit',20))
                if not 0 <= limit <= 100 or not 0 <= controls <= 100:
                    raise ValueError('Invalid pull limit')
                updates = self.db.pull_pending_updates(min(limit,agent['inbox_capacity']),controls)
                return 200, {'ok':True,'updates':updates,'ready':True}
            if method == 'POST' and path == '/agent/ack':
                ids = body.get('update_ids')
                if not isinstance(ids,list):
                    raise ValueError('Invalid update_ids')
                return 200, {'ok':True,'acked':self.db.ack_updates(ids)}
        except (ValueError, TypeError):
            return 400, {'ok':False,'error':'Invalid request parameters'}
        return 404, {'ok':False,'error':'Not found'}

    async def start(self):
        self.server = await asyncio.start_server(self._handle_client,self.cfg.agent_api_host,self.cfg.agent_api_port,limit=16384)
        self._running = True

    async def serve(self):
        if not self.server:
            raise RuntimeError('Agent API has not started')
        await self.server.serve_forever()

    async def stop(self):
        self._running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
