"""Check that this computer reaches the node's network directly, not through a proxy or TUN.

A domestic echo service tells the address this computer goes out from, and the node tells the
address it sees the SSH login come from. The scan stays in the node's /20, so when the two
agree, the scan leaves from the same address. A proxy that carries the domestic echo too shows
its own address on both sides, so DNS is also checked: proxies in TUN mode (Quantumult X,
Surge, Clash and others) answer every name with a fake address from 198.18.0.0/15, which public
DNS never does.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

# Tried in order; the first that answers with an address counts.
DOMESTIC_ECHOES = ("https://4.ipw.cn", "https://ipv4.ddnspod.com")
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class Egress:
    """This computer's address as a domestic echo sees it and as the node sees it (``None`` when
    unknown), and whether this computer's DNS answers with a proxy's fake addresses."""

    domestic: str | None
    node: str | None
    fake_dns: bool = False


def direct_address(transport: httpx.BaseTransport | None = None) -> str | None:
    """The address the first domestic echo service that answers sees this computer connect from.

    System proxy variables are ignored so that the answer is about the network path.
    """
    with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False) as client:
        for url in DOMESTIC_ECHOES:
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            if response.is_success and (address := _address(response.text)) is not None:
                return address
    return None


def fake_ip_dns(resolve: Callable = socket.getaddrinfo) -> bool:
    """Whether this computer's DNS answers the echo services' names with fake 198.18.0.0/15 addresses."""
    for url in DOMESTIC_ECHOES:
        try:
            answers = resolve(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            continue
        if any(ipaddress.ip_address(answer[4][0]) in FAKE_IP_NETWORK for answer in answers):
            return True
    return False


def client_address(ssh_client: str) -> str | None:
    """The client address in the node's ``SSH_CLIENT`` ("<address> <port> <port>"), or ``None``."""
    words = ssh_client.split()
    address = _address(words[0]) if words else None
    if address is not None and (ip := ipaddress.ip_address(address)).version == 6 and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return address


def is_direct(e: Egress) -> bool | None:
    """True when both addresses are the same; ``None`` when either is unknown; False behind fake DNS."""
    if e.fake_dns:
        return False
    if e.domestic is None or e.node is None:
        return None
    return e.domestic == e.node


def _address(text: str | None) -> str | None:
    """``text`` as a canonical IP address, or ``None`` when it is not one."""
    try:
        return str(ipaddress.ip_address((text or "").strip()))
    except ValueError:
        return None
