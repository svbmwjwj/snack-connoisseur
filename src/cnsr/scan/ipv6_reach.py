"""Test from this computer whether a node's IPv6 address answers on 443.

The control plane's Worker cannot open connections to any IPv6 address, so IPv6 is tested
from here. A failed connection proves the node unreachable only when this computer reaches
IPv6 at all: public addresses that always answer on 443 are tried next, and when none of them
answers either, the result says the node was not tested.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

PORT = 443
TIMEOUT_SECONDS = 5.0
# Public DNS that serves DNS over HTTPS on 443: Alibaba's first, since mainland China blocks
# Cloudflare's and Google's on 443 (seen from a mainland phone hotspot, 2026-10-19) while it
# reaches Alibaba's, which answers from abroad too.
REFERENCES = ("2400:3200::1", "2400:3200:baba::1", "2606:4700:4700::1111", "2001:4860:4860::8888")


@dataclass(frozen=True)
class Ipv6Reach:
    """One connection attempt to 443. ``tested`` is False when this computer reaches no IPv6
    address at all, so a failure says nothing about the node."""

    ok: bool
    ms: int | None
    error: str | None
    tested: bool = True


def probe_ipv6(host: str, connect: Callable[[str], int] | None = None) -> Ipv6Reach:
    """Connect to ``host`` on 443; on failure, check that IPv6 works here at all."""
    connect = connect or _connect
    try:
        ms = connect(host)
    except OSError as exc:
        error = _reason(exc)
    else:
        return Ipv6Reach(ok=True, ms=ms, error=None)
    for reference in REFERENCES:
        try:
            connect(reference)
        except OSError:
            continue
        return Ipv6Reach(ok=False, ms=None, error=error)
    return Ipv6Reach(ok=False, ms=None, error=f"这台电脑连不上任何 IPv6 地址：{error}", tested=False)


def _connect(host: str) -> int:
    """Open and close a TCP connection to ``host`` on 443; return how long it took in ms."""
    start = time.monotonic()
    with socket.create_connection((host, PORT), timeout=TIMEOUT_SECONDS):
        pass
    return round((time.monotonic() - start) * 1000)


def _reason(exc: OSError) -> str:
    if isinstance(exc, TimeoutError):
        return "连接超时"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝"
    return exc.strerror or str(exc) or type(exc).__name__
