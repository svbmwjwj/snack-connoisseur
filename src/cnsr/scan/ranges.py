"""Which addresses around the node are scanned."""

from __future__ import annotations

import ipaddress


def scan_network(ipv4: str) -> str:
    """The node's /20, the network the scan stays in, as ``a.b.c.d/20``."""
    return str(ipaddress.ip_network(f"{ipaddress.IPv4Address(ipv4)}/20", strict=False))


def scan_targets(ipv4: str) -> list[str]:
    """Every host address in the node's /20 except the ones in the node's own /22.

    The /20's network and broadcast addresses are never returned. The list is in ascending order.
    """
    node = ipaddress.IPv4Address(ipv4)
    block = ipaddress.ip_network(scan_network(ipv4))
    own = ipaddress.ip_network(f"{node}/22", strict=False)
    return [str(address) for address in block.hosts() if address not in own]
