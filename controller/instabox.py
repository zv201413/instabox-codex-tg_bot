"""
InstaBox 原生 REST API 客户端
使用标准库 urllib.request 实现，避免向子进程暴露敏感凭据。
遵循官方生命周期规约：
- 暂停只调 resume，绝不随意回退 start 破坏暂停状态
- 严格解析 409/429 冷却与重试信息（cooldownUntil / retryAfterSeconds / Retry-After）
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("instabox")


class InstaBoxError(Exception):
    def __init__(self, message: str, status_code: int = 0, body: Optional[Dict[str, Any]] = None,
                 cooldown_until: Optional[str] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.cooldown_until = cooldown_until
        self.retry_after = retry_after
        detail = self.body.get('error')
        self.code = (detail.get('code') if isinstance(detail, dict) else detail) or self.body.get('code') or ''


class InstaBoxClient:
    def __init__(self, api_url: str, api_key: str, timeout: int = 30):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
        url = f"{self.api_url}{path}"
        req_headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "InstaBox-Controller/1.0"
        }
        data_bytes = None
        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            data_bytes = json.dumps(payload).encode("utf-8")

        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body_raw = resp.read().decode("utf-8")
                try:
                    res_json = json.loads(body_raw) if body_raw else {}
                except Exception:
                    res_json = {"raw": body_raw}
                if not isinstance(res_json, dict):
                    raise InstaBoxError('Invalid API response', status_code=502)
                return status, res_json
        except urllib.error.HTTPError as e:
            status = e.code
            body_raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                res_json = json.loads(body_raw) if body_raw else {}
            except Exception:
                res_json = {"raw": body_raw}

            if not isinstance(res_json, dict):
                res_json = {}
            detail = res_json.get('error')
            detail = detail if isinstance(detail, dict) else {}
            retry_after = None
            retry_header = e.headers.get('Retry-After', '') if e.headers else ''
            try:
                retry_after = max(0, int(retry_header))
            except (ValueError, TypeError):
                try:
                    date = parsedate_to_datetime(retry_header)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    retry_after = max(0, int((date - datetime.now(timezone.utc)).total_seconds()) + 1)
                except (ValueError, TypeError, OverflowError):
                    pass
            body_retry = res_json.get('retryAfterSeconds', detail.get('retryAfterSeconds'))
            try:
                body_retry = max(0, int(body_retry))
                retry_after = max(retry_after or 0, body_retry)
            except (ValueError, TypeError):
                pass
            cooldown_until = res_json.get('cooldownUntil') or detail.get('cooldownUntil')
            # Never log response bodies or request headers: a proxy can echo credentials.
            msg = f'HTTP {status} {method} {path}'
            raise InstaBoxError(msg, status_code=status, body=res_json,
                                cooldown_until=cooldown_until, retry_after=retry_after) from e
        except urllib.error.URLError as e:
            raise InstaBoxError('Network request failed', status_code=0) from e
        except (TimeoutError, OSError) as e:
            raise InstaBoxError('Network request timed out or failed', status_code=0) from e

    # ---- 异步包装 ----
    async def list_machines(self) -> List[Dict[str, Any]]:
        status, data = await asyncio.to_thread(self._request, "GET", "/v1/machines")
        return data.get("machines", [])

    async def get_machine(self, machine_id: str) -> Dict[str, Any]:
        status, data = await asyncio.to_thread(self._request, "GET", f"/v1/machines/{machine_id}")
        return data

    async def resolve_machine_id(self, target: str) -> str:
        """如果 target 已经是 m_ 开头或明确 id 则直接返回，否则匹配 name"""
        if not target or target == "all":
            raise ValueError("必须指定单台具体机器，不支持空或 all")
        if target.startswith("m_") and len(target) > 10:
            return target
        machines = await self.list_machines()
        for m in machines:
            if m.get("name") == target or m.get("id") == target:
                return m["id"]
        raise ValueError(f"名下未找到匹配名称或 ID 的机器: {target}")

    async def put_lease(self, machine_id: str, lease_name: str, ttl_seconds: int) -> Dict[str, Any]:
        """续租或创建 Activity Lease"""
        status, data = await asyncio.to_thread(
            self._request, "PUT", f"/v1/machines/{machine_id}/activity-leases/{lease_name}",
            {"ttlSeconds": ttl_seconds}
        )
        return data

    async def delete_lease(self, machine_id: str, lease_name: str) -> bool:
        """释放 Activity Lease"""
        try:
            status, _ = await asyncio.to_thread(
                self._request, "DELETE", f"/v1/machines/{machine_id}/activity-leases/{lease_name}"
            )
            return status in (200, 204)
        except InstaBoxError as e:
            if e.status_code == 404:
                return True
            raise

    async def resume_machine(self, machine_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """发起恢复机器"""
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        headers = {"Idempotency-Key": idempotency_key}
        status, data = await asyncio.to_thread(
            self._request, "POST", f"/v1/machines/{machine_id}/resume",
            payload={}, headers=headers
        )
        return data

    async def start_machine(self, machine_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """启动 stopped 机器（仅在明确状态且允许时调用）"""
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        headers = {"Idempotency-Key": idempotency_key}
        status, data = await asyncio.to_thread(
            self._request, "POST", f"/v1/machines/{machine_id}/start",
            payload={}, headers=headers
        )
        return data

    async def suspend_machine(self, machine_id: str, idempotency_key: str) -> Dict[str, Any]:
        _, data = await asyncio.to_thread(self._request, 'POST', f'/v1/machines/{machine_id}/suspend',
                                         payload={}, headers={'Idempotency-Key': idempotency_key})
        return data

    async def get_operation(self, operation_id: str) -> Dict[str, Any]:
        status, data = await asyncio.to_thread(self._request, "GET", f"/v1/operations/{operation_id}")
        return data
