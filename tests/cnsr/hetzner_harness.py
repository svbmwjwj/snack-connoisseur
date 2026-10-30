"""A real HetznerClient whose requests a scripted router answers (no network, no real token)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from cnsr.hetzner.api import HetznerClient
from cnsr.hetzner.cloud import Hetzner

TOKEN = "hz-test-token-not-real"
LOCATION = "fsn1"
TB = 1024**4

Answer = dict | httpx.Response | Callable[[httpx.Request], httpx.Response]


def done(command: str = "x", action_id: int = 1) -> dict:
    return {"id": action_id, "command": command, "status": "success", "error": None}


def fail(status: int, code: str, message: str = "detail") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": {}}})


def server(name: str, server_id: int = 10, status: str = "running", alias: str | None = None, **extra) -> dict:
    """A server as the API lists it; labelled ``cnsr=<alias>`` (the name by default), ``alias=""`` for none."""
    alias = name if alias is None else alias
    return {
        "id": server_id,
        "name": name,
        "status": status,
        "labels": {"cnsr": alias} if alias else {},
        "public_net": {"ipv4": None, "ipv6": {"id": 5, "ip": "2001:db8:1::/64"}},
        **extra,
    }


def primary_ip(name: str, ip_id: int = 20, alias: str = "de-1", assignee: int | None = None, **extra) -> dict:
    return {
        "id": ip_id,
        "name": name,
        "ip": "203.0.113.7",
        "type": "ipv4",
        "labels": {"cnsr": alias} if alias else {},
        "location": {"name": LOCATION},
        "assignee_type": "server",
        "assignee_id": assignee,
        "auto_delete": True,
        **extra,
    }


def firewall(name: str, fw_id: int = 30, alias: str = "de-1", servers: tuple[int, ...] = ()) -> dict:
    return {
        "id": fw_id,
        "name": name,
        "labels": {"cnsr": alias} if alias else {},
        "rules": [],
        "applied_to": [{"type": "server", "server": {"id": sid}} for sid in servers],
    }


def price(location: str, monthly: str, traffic: int = 20 * TB, per_tb: str = "1.1900") -> dict:
    return {
        "location": location,
        "price_hourly": {"net": "0", "gross": "0"},
        "price_monthly": {"net": "0", "gross": monthly},
        "included_traffic": traffic,
        "price_per_tb_traffic": {"net": "1", "gross": per_tb},
    }


def stype(name: str, cores: float, memory: float, monthly: str = "5.0000", *, disk: float = 40,
          architecture: str = "x86", cpu_type: str = "shared", available: bool = True,
          deprecation: dict | None = None, place_deprecation: dict | None = None,
          location: str = "fsn1", deprecated: bool = False, category: str = "regular_purpose") -> dict:
    return {
        "id": 1,
        "name": name,
        "cores": cores,
        "memory": memory,
        "disk": disk,
        "deprecated": deprecated,
        "architecture": architecture,
        "cpu_type": cpu_type,
        "category": category,
        "deprecation": deprecation,
        "prices": [price(location, monthly)],
        "locations": [{"id": 1, "name": location, "available": available, "deprecation": place_deprecation}],
    }


def pricing(location: str = "fsn1", ip: str = "0.6050", currency: str = "EUR") -> dict:
    return {
        "currency": currency,
        "primary_ips": [
            {"type": "ipv6", "prices": [{"location": location, "price_monthly": {"net": "0", "gross": "0"}}]},
            {"type": "ipv4", "prices": [{"location": location, "price_monthly": {"net": "0.5", "gross": ip}}]},
        ],
    }


class Api:
    """Answers ``(method, path)`` from queues: each answer is used once, the last one again and again.

    A request nobody scripted fails the test. ``calls`` records ``(method, path, params, body)``.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Answer]] = {}
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.now = 0.0
        self.sleeps: list[float] = []
        client = HetznerClient(TOKEN, transport=httpx.MockTransport(self._handle), sleep=self._sleep, clock=self._clock)
        self.hz = Hetzner("de-falkenstein", client, sleep=self._sleep, clock=self._clock)

    def on(self, method: str, path: str, *answers: Answer) -> None:
        self.routes.setdefault((method, path), []).extend(answers)

    def sent(self, method: str, path: str) -> list[Any]:
        """The bodies of the requests made to ``method path``."""
        return [body for m, p, _, body in self.calls if (m, p) == (method, path)]

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _, _ in self.calls]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params), body))
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError(f"unscripted request {request.method} {path}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def _clock(self) -> float:
        return self.now
