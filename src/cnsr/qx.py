"""The client lines cnsr prints and serves: one node line, the Quantumult X config sections, and the
v2rayN subscription address."""

from __future__ import annotations

from cnsr.state import ControlMeta, XrayInfo


def node_line(domain: str, x: XrayInfo, alias: str) -> str:
    """The server line of one node, as the control plane serves it in ``cnsr.snippet``.

    There is no ``fast-open``: REALITY does not work with TCP Fast Open.
    """
    return (
        f"vless={domain}:443, method=none, password={x.uuid}, obfs=over-tls, obfs-host={x.sni}, "
        f"reality-base64-pubkey={x.public_key}, reality-hex-shortid={x.short_id}, "
        f"vless-flow=xtls-rprx-vision, udp-relay=true, tag={alias}"
    )


def check_url(meta: ControlMeta) -> str:
    """The node check script a Quantumult X task runs (every minute, in each device's config)."""
    return f"https://{meta.host}/{meta.client_token}/check.js"


def check_tasks(meta: ControlMeta) -> list[str]:
    """The ``[task_local]`` lines of a device's Quantumult X config: the check every minute, then
    the one a long press on a node runs."""
    check = check_url(meta)
    return [
        f"* * * * * {check}, tag=cnsr 节点检测, enabled=true",
        f"event-interaction {check.removesuffix('.js')}-ui.js, tag=cnsr 检测, enabled=true",
    ]


def snippet_url(meta: ControlMeta) -> str:
    """The Quantumult X subscription address: the server lines of every node."""
    return f"https://{meta.host}/{meta.client_token}/cnsr.snippet"


def v2rayn_url(meta: ControlMeta) -> str:
    """The v2rayN subscription address: the same nodes as ``cnsr.snippet``, as share links."""
    return f"https://{meta.host}/{meta.client_token}/v2rayn"
