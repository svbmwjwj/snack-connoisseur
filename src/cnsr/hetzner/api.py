"""Thin client for the Hetzner Cloud REST API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.waiting import Deadline

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hetzner.cloud/v1"
TIMEOUT_SECONDS = 30.0
PER_PAGE = 50
POLL_SECONDS = 2

_RETRY_LATER = "Hetzner 服务出错，请稍后重试"
# Every known code gets a fixed text; the Hetzner text is kept for the rest.
_REASONS = {
    "unauthorized": "Hetzner API token 无效或已被删除，请检查 HCLOUD_TOKEN",
    "forbidden": "token 没有写权限（只读 token），请在 Hetzner 控制台新建一个读写（Read & Write）token",
    "maintenance": "Hetzner 正在维护，请稍后重试",
    "resource_limit_exceeded": "已达到 Hetzner 账号的资源数量上限，请删掉不用的资源，或向 Hetzner 申请提高上限",
    "not_found": "资源不存在",
    "uniqueness_error": "名字或内容与项目里已有的资源重复",
    "conflict": "资源在请求期间发生了变化，请重试",
    "resource_unavailable": "所需资源暂时无货或不可购买，请换一个规格或区域",
    "placement_error": "Hetzner 暂时无法在这个位置放置服务器（多为无货），请换一个规格或区域",
    "locked": "资源上还有动作在进行，请稍后重试",
    "server_not_stopped": "服务器还在运行，这个操作要求先关机",
    "rate_limit_exceeded": "请求太频繁，已超过 Hetzner 的限额，请稍后重试",
    "server_error": _RETRY_LATER,
    "bad_gateway": _RETRY_LATER,
    "unavailable": _RETRY_LATER,
    "timeout": _RETRY_LATER,
}


class HetznerError(CnsrError):
    """An API call, or an action it started, that failed.

    ``status`` is the HTTP status, or 0 when no response arrived or when an action failed
    after the call that started it succeeded. ``code`` and ``message`` are Hetzner's own
    (``code`` is '' when none came back); ``command`` names the failed action. The text
    never contains the token.
    """

    def __init__(self, status: int, code: str, message: str, command: str | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.command = command
        reason = reason_for(code, message)
        if command is not None:
            text = f"Hetzner 动作失败（{command}）：{reason}"
        elif status == 0:
            text = f"无法连接 Hetzner API：{reason}"
        else:
            text = f"Hetzner API 请求失败（HTTP {status}）：{reason}"
        super().__init__(text)


class HetznerClient:
    """Authenticated access to ``https://api.hetzner.cloud/v1``.

    ``sleep`` and ``clock`` are used while waiting for an action to finish.
    """

    def __init__(
        self,
        token: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        runlog.secret(token)
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=TIMEOUT_SECONDS,
        )
        self._sleep, self._clock = sleep, clock

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        """Call the API and return the parsed JSON body, or None when the body is empty."""
        began = time.monotonic()
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.TransportError as exc:
            reason = "请求超时" if isinstance(exc, httpx.TimeoutException) else "网络连接失败"
            logger.info("Hetzner %s %s → %s，耗时 %.1f 秒", method, path, reason, time.monotonic() - began)
            raise HetznerError(0, "", reason) from exc
        took = time.monotonic() - began
        logger.info("Hetzner %s %s → HTTP %s，耗时 %.1f 秒", method, path, response.status_code, took)
        if not response.is_success:
            error = _error(response)
            raise HetznerError(response.status_code, str(error.get("code") or ""), str(error.get("message") or ""))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise HetznerError(response.status_code, "", "响应不是有效的 JSON") from exc

    def pages(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Every item under ``key`` across all pages of a list endpoint."""
        items: list[Any] = []
        number: Any = 1
        while number:
            body = self.request("GET", path, params={**(params or {}), "page": number, "per_page": PER_PAGE}) or {}
            items.extend(body.get(key) or [])
            number = ((body.get("meta") or {}).get("pagination") or {}).get("next_page")
        return items

    def wait_action(self, action: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
        """Poll every ``POLL_SECONDS`` until ``action`` succeeds and return it; ``HetznerError`` when it fails."""
        deadline = Deadline(timeout, self._clock)
        while action.get("status") == "running":
            if deadline.passed():
                raise CnsrError(
                    f"等了 {timeout} 秒，Hetzner 动作 {action.get('command')} 还没有完成，请稍后在 Hetzner 控制台查看。"
                )
            self._sleep(deadline.step(POLL_SECONDS))
            action = self.request("GET", f"/actions/{action['id']}")["action"]
        if action.get("status") == "error":
            error = action.get("error") or {}
            raise HetznerError(0, str(error.get("code") or ""), str(error.get("message") or ""), str(action.get("command")))
        return action


def reason_for(code: str, message: str) -> str:
    """The reason shown for a Hetzner error code: a fixed text, or the Hetzner text for other codes."""
    if code in _REASONS:
        return _REASONS[code]
    if code:
        return f"Hetzner 返回 {code}：{message or '没有详情'}"
    return message or "未返回错误详情"


def _error(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}
