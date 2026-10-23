"""Thin client for the Cloudflare REST API."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from cnsr.errors import CnsrError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 30.0


class CloudflareError(CnsrError):
    """An API call that failed.

    ``status`` is the HTTP status, or 0 when no response arrived. ``messages``
    holds Cloudflare's own error messages and ``codes`` their numeric codes. The text never
    contains the token.
    """

    def __init__(self, status: int, messages: list[str], codes: list[int] | None = None):
        self.status = status
        self.messages = list(messages)
        self.codes = list(codes or [])
        detail = "；".join(self.messages) if self.messages else "未返回错误详情"
        if status == 0:
            text = f"无法连接 Cloudflare API：{detail}"
        else:
            text = f"Cloudflare API 请求失败（HTTP {status}）：{detail}"
        super().__init__(text)


class CloudflareClient:
    """Authenticated access to ``https://api.cloudflare.com/client/v4``."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=TIMEOUT_SECONDS,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        raw: bool = False,
    ) -> Any:
        """Call the API and return the envelope's ``result``.

        With ``raw=True`` the unwrapped ``httpx.Response`` is returned instead
        (KV values come back as the plain body, not as an envelope). Failures
        raise ``CloudflareError`` in both modes.
        """
        began = time.monotonic()
        try:
            response = self._client.request(method, path, params=params, json=json, files=files)
        except httpx.TransportError as exc:
            reason = "请求超时" if isinstance(exc, httpx.TimeoutException) else "网络连接失败"
            logger.info("Cloudflare %s %s → %s，耗时 %.1f 秒", method, path, reason, time.monotonic() - began)
            raise CloudflareError(0, [reason]) from exc
        took = time.monotonic() - began
        logger.info("Cloudflare %s %s → HTTP %s，耗时 %.1f 秒", method, path, response.status_code, took)
        if not response.is_success:
            body = _json_object(response)
            raise CloudflareError(response.status_code, _error_messages(body), _error_codes(body))
        if raw:
            return response
        return envelope(response).get("result")

    def zone(self, name: str) -> dict[str, Any]:
        """Look up a zone by name; returns ``{"id": ..., "account": {"id": ...}}``."""
        found = self.request("GET", "/zones", params={"name": name})
        if not found:
            raise CnsrError(f"在 Cloudflare 账号里找不到域名 {name}，请确认域名已添加，且令牌有读取域名的权限。")
        zone = found[0]
        return {"id": zone["id"], "account": {"id": zone["account"]["id"]}}


def envelope(response: httpx.Response) -> dict[str, Any]:
    """The JSON envelope of a successful response; ``CloudflareError`` when it is not one."""
    body = _json_object(response)
    if body is None:
        raise CloudflareError(response.status_code, ["响应不是有效的 JSON"])
    if body.get("success") is False:
        raise CloudflareError(response.status_code, _error_messages(body), _error_codes(body))
    return body


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _errors(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    errors = (body or {}).get("errors")
    return [error for error in errors if isinstance(error, dict)] if isinstance(errors, list) else []


def _error_messages(body: dict[str, Any] | None) -> list[str]:
    return [str(error["message"]) for error in _errors(body) if "message" in error]


def _error_codes(body: dict[str, Any] | None) -> list[int]:
    return [error["code"] for error in _errors(body) if isinstance(error.get("code"), int)]
