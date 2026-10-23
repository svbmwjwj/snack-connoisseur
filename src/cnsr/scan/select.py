"""Ranking the SNI candidates a node confirmed, and the ``backups.json`` file the node keeps.

``validate-candidates.sh`` prints ``domain ip ms`` for every candidate that completes a TLS 1.3
+ h2 request from the node itself. ``parse_validation`` reads that, ``choose`` picks the
fastest as the current SNI and keeps the next ones as backups. ``backups_json`` and
``parse_backups`` are the one format of ``/opt/cnsr/backups.json``: a JSON array of
``{"domain", "ip", "latency_ms"}`` objects, best first, without the current SNI.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass

from cnsr.errors import CnsrError

_DOMAIN = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")
_MILLISECONDS = re.compile(r"[0-9]+")
_NO_CANDIDATE = "没有候选通过节点验证，已停止，不会盲选"


@dataclass(frozen=True)
class Candidate:
    """A site on the node's network that answered from the node, and how fast (TLS connect, ms)."""

    domain: str
    ip: str
    latency_ms: int


def parse_validation(text: str) -> list[Candidate]:
    """Read ``domain ip ms`` lines in order; a line in any other shape is ignored."""
    found: list[Candidate] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        domain, ip, milliseconds = fields
        if _DOMAIN.fullmatch(domain) and _is_ip(ip) and _MILLISECONDS.fullmatch(milliseconds):
            found.append(Candidate(domain, ip, int(milliseconds)))
    return found


def choose(cands: list[Candidate], max_backups: int = 10) -> tuple[Candidate, list[Candidate]]:
    """The fastest candidate and up to ``max_backups`` next-fastest ones.

    Equal latencies keep their input order. With nothing to choose from this stops instead of
    guessing: a blind pick could be a site the node cannot reach.
    """
    if not cands:
        raise CnsrError(_NO_CANDIDATE)
    ranked = sorted(cands, key=lambda cand: cand.latency_ms)
    return ranked[0], ranked[1 : 1 + max(max_backups, 0)]


def backups_json(backups: list[Candidate]) -> str:
    """The text of ``backups.json`` for these backups, best first."""
    entries = [{"domain": b.domain, "ip": b.ip, "latency_ms": b.latency_ms} for b in backups]
    return json.dumps(entries, indent=2, ensure_ascii=False) + "\n"


def parse_backups(text: str) -> list[Candidate]:
    """Read ``backups.json``; anything that is not the format of ``backups_json`` is an error."""
    try:
        data = json.loads(text.removeprefix("\ufeff"))
    except ValueError:
        raise CnsrError("备用 SNI 列表不是有效的 JSON。") from None
    if not isinstance(data, list):
        raise CnsrError("备用 SNI 列表应当是一个 JSON 数组。")
    return [_backup(number, entry) for number, entry in enumerate(data, start=1)]


def _backup(number: int, entry: object) -> Candidate:
    bad = CnsrError(f"备用 SNI 列表第 {number} 项格式不对，应为 domain、ip、latency_ms。")
    if not isinstance(entry, dict):
        raise bad
    domain, ip, latency = entry.get("domain"), entry.get("ip"), entry.get("latency_ms")
    if not (isinstance(domain, str) and _DOMAIN.fullmatch(domain) and isinstance(ip, str) and _is_ip(ip)):
        raise bad
    if not isinstance(latency, int) or isinstance(latency, bool) or latency < 0:
        raise bad
    return Candidate(domain, ip, latency)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
