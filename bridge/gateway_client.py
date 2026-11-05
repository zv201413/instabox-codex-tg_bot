"""Fixed-machine outbound transport and durable handoff into a Bridge inbox."""
import json
import urllib.request
from urllib.parse import urlsplit

from controller.config import private_host


class GatewayClient:
    def __init__(self, gateway_url, secret, machine_id, agent_id='codex-bridge', timeout=15):
        url = urlsplit(gateway_url)
        if url.username or url.password or url.scheme not in ('http','https') or not url.hostname:
            raise ValueError('Invalid gateway URL')
        if url.scheme == 'http' and not private_host(url.hostname):
            raise ValueError('Use HTTPS or a private IP/localhost for the agent endpoint')
        if not secret or not machine_id:
            raise ValueError('Gateway requires authentication and fixed machine binding')
        self.gateway_url, self.secret = gateway_url.rstrip('/'), secret
        self.machine_id, self.agent_id, self.timeout = machine_id, agent_id, timeout

    def _post(self, path, payload):
        payload = {**payload,'machine_id':self.machine_id,'agent_id':self.agent_id}
        request = urllib.request.Request(self.gateway_url+path,data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json','X-Agent-Secret':self.secret},method='POST')
        with urllib.request.urlopen(request,timeout=self.timeout) as response:
            result = json.loads(response.read())
            if not result.get('ok'):
                raise RuntimeError('Gateway rejected request')
            return result

    def pull(self, limit=10, control_limit=20):
        return self._post('/agent/pull',dict(limit=limit,control_limit=control_limit)).get('updates',[])

    def ack(self, update_ids):
        if update_ids:
            self._post('/agent/ack',dict(update_ids=update_ids))
        return True

    def heartbeat(self, agent_id=None, version='1', codex_status='ready', current_task=None, inbox_capacity=10):
        if agent_id and agent_id != self.agent_id:
            raise ValueError('Bridge identity cannot change per request')
        self._post('/agent/heartbeat',dict(version=version,codex_status=codex_status,current_task=current_task,inbox_capacity=inbox_capacity))
        return True

    def receive_into(self, inbox, context=None, limit=10, control_limit=20):
        """Commit complete batch locally before acking. A failed ack safely redelivers it."""
        updates = self.pull(limit,control_limit)
        inbox.ingest(updates,context or {})
        self.ack([update['update_id'] for update in updates])
        return len(updates)
