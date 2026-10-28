"""What ``cnsr up`` can put on a server: the base hardening, and extensions that need it.

The base comes first and every extension depends on it. A server's record lists the features
a finished run left on it (``Node.features``).
"""

from __future__ import annotations

from cnsr.server import xray
from cnsr.state import Node

HARDEN = "harden"
XRAY = "xray"
BASE = HARDEN
EXTENSIONS = (XRAY,)
ALL = (BASE, *EXTENSIONS)
LABELS = {HARDEN: "基础加固", XRAY: "Xray 节点（REALITY，SNI 失效时自动更换）"}
# What each feature shows as in ``cnsr list``.
SHORT_LABELS = {HARDEN: "仅加固", XRAY: "Xray 节点"}
APP_PORTS = {XRAY: xray.PORTS}


def installed(node: Node | None) -> list[str]:
    """The features a finished run left on ``node``, in ``ALL`` order.

    A record from before features were recorded counts as both when it has a client line.
    """
    if node is None:
        return []
    if node.features:
        return [feature for feature in ALL if feature in node.features]
    return list(ALL) if node.line is not None else []


def problem(chosen: list[str]) -> str | None:
    """Why ``chosen`` cannot be deployed, or None when it can."""
    if not chosen:
        return f"至少选择一项；{LABELS[BASE]}是其他功能的基础。"
    if BASE not in chosen:
        return f"选了扩展功能就必须同时选择{LABELS[BASE]}。"
    return None


def app_ports(chosen: list[str]) -> tuple[tuple[int, str], ...]:
    """The ``(port, protocol)`` pairs the chosen features listen on, without repeats."""
    ports: list[tuple[int, str]] = []
    for feature in chosen:
        ports += [port for port in APP_PORTS.get(feature, ()) if port not in ports]
    return tuple(ports)


def summary(chosen: list[str]) -> str:
    """How ``cnsr list`` names what a server has."""
    extensions = [SHORT_LABELS[feature] for feature in chosen if feature in EXTENSIONS]
    return "、".join(extensions) if extensions else SHORT_LABELS[HARDEN]
