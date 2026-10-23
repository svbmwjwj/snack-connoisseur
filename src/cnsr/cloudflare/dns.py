"""DNS record management and resolution checks for node domains."""

from __future__ import annotations

import ipaddress
import random
import socket
from typing import Literal

import httpx

from cnsr.cloudflare.api import CloudflareClient, CloudflareError
from cnsr.errors import CnsrError
from cnsr.naming import random_label

RECORD_TTL = 60
# Random names drawn before giving up when every one is taken.
FRESH_NAME_ATTEMPTS = 5
DOH_TIMEOUT_SECONDS = 10.0
_DNS_TYPE_CODES = {"A": 1, "AAAA": 28}
_DNS_JSON = {"accept": "application/dns-json"}
# Public resolvers with a JSON API, asked in this order: Cloudflare by name, Cloudflare by
# address (for networks that block the name), then AliDNS (for networks that block both).
# Each entry is the URL, whether the type goes as its name or its number, and the headers.
PUBLIC_RESOLVERS = (
    ("https://cloudflare-dns.com/dns-query", "name", _DNS_JSON),
    ("https://1.1.1.1/dns-query", "name", _DNS_JSON),
    ("https://dns.alidns.com/resolve", "code", {}),
)


def upsert_record(
    cf: CloudflareClient, zone_id: str, name: str, rtype: Literal["A", "AAAA"], content: str, *, comment: str
) -> str:
    """Make ``name`` point at ``content`` as a grey-cloud record with a 60 second TTL; returns the record id.

    ``comment`` is the note Cloudflare shows beside the record. An existing record of that
    type is corrected where it differs: its content, a proxied flag or TTL someone changed,
    and a missing comment (one someone wrote is kept). One that is already right is left untouched.
    """
    path = f"/zones/{zone_id}/dns_records"
    existing = cf.request("GET", path, params={"type": rtype, "name": name})
    if not existing:
        body = {"type": rtype, "name": name, "content": content, "ttl": RECORD_TTL, "proxied": False}
        return cf.request("POST", path, json=body | {"comment": comment})["id"]
    record = existing[0]
    patch = {}
    if _normalize(record.get("content", "")) != _normalize(content):
        patch["content"] = content
    if record.get("proxied") is not False or record.get("ttl") != RECORD_TTL:
        patch.update(proxied=False, ttl=RECORD_TTL)
    if not record.get("comment"):
        patch["comment"] = comment
    if patch:
        cf.request("PATCH", f"{path}/{record['id']}", json=patch)
    return record["id"]


def fresh_name(cf: CloudflareClient, zone_id: str, zone: str, rng: random.Random) -> str:
    """A random name under ``zone`` that has no DNS record of any type yet.

    A name that is taken belongs to someone else's record and is never adopted; another is
    drawn, up to ``FRESH_NAME_ATTEMPTS`` times.
    """
    for _ in range(FRESH_NAME_ATTEMPTS):
        name = f"{random_label(rng)}.{zone}"
        if not cf.request("GET", f"/zones/{zone_id}/dns_records", params={"name": name}):
            return name
    raise CnsrError(f"随机生成的 {FRESH_NAME_ATTEMPTS} 个域名在 {zone} 下都已有解析记录，请稍后重跑。")


def delete_record(cf: CloudflareClient, zone_id: str, record_id: str) -> None:
    """Delete a record; one that is already gone is not an error."""
    try:
        cf.request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
    except CloudflareError as exc:
        if exc.status != 404:
            raise


def public_resolves(name: str, rtype: str, expected: set[str], transport: httpx.BaseTransport | None = None) -> bool:
    """True when a public resolver answers exactly ``expected`` for ``name``.

    The resolvers of ``PUBLIC_RESOLVERS`` are asked in order until one gives that answer. A
    lookup that cannot be completed counts as not resolving, so False means that none of
    them answered as expected, and callers can simply poll.
    """
    code = _DNS_TYPE_CODES.get(rtype)
    if code is None:
        raise ValueError(f"unsupported record type: {rtype}")
    wanted = {_normalize(address) for address in expected}
    with httpx.Client(transport=transport, timeout=DOH_TIMEOUT_SECONDS) as client:
        for url, type_form, headers in PUBLIC_RESOLVERS:
            params = {"name": name, "type": rtype if type_form == "name" else str(code)}
            if _answer(client, url, params, headers, code) == wanted:
                return True
    return False


def _answer(client: httpx.Client, url: str, params: dict, headers: dict, code: int) -> set[str] | None:
    """The addresses of type ``code`` one resolver answers with; None when the lookup failed."""
    try:
        response = client.get(url, params=params, headers=headers)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    answers = body.get("Answer") if isinstance(body, dict) else None
    return {_normalize(str(a.get("data"))) for a in answers or [] if isinstance(a, dict) and a.get("type") == code}


def local_resolves(name: str, expected: set[str]) -> bool:
    """True when this machine resolves ``name`` to the node's IPv4 address and to nothing but ``expected``.

    ``expected`` holds the node's addresses. An IPv6 address may be missing from the answer,
    since some local resolvers filter AAAA records; an address of another server may not.
    """
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    resolved = {_normalize(info[4][0]) for info in infos}
    wanted = {_normalize(address) for address in expected}
    ipv4 = {address for address in wanted if ":" not in address}
    return bool(resolved) and ipv4 <= resolved and resolved <= wanted


def _normalize(address: str) -> str:
    """Canonical text for an IP address, so IPv6 spellings compare equal."""
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        return address
