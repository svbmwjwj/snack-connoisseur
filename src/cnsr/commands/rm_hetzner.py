"""``cnsr rm`` for a Hetzner node: its server, then its Primary IP, then its firewall.

Each resource is deleted only when it carries the label ``cnsr=<alias>``, so names the record
lacks (a run killed right after a create) are looked up by the names cnsr gives them. The
SSH key stays: other servers in the project may use it.
"""

from __future__ import annotations

from cnsr.deploy.context import Deps, hetzner_elsewhere, known_hetzner
from cnsr.deploy.hetzner_host import FIREWALL_SUFFIX, STATIC_IP_SUFFIX
from cnsr.errors import CnsrError
from cnsr.hetzner.cloud import Hetzner
from cnsr.hetzner.labels import TAG_KEY, server_name
from cnsr.state import Node


def show_plan(node: Node, deps: Deps) -> str:
    """List the Hetzner resources ``rm`` deletes; return what stays."""
    ui = deps.ui
    ui.info(f"  — Hetzner 服务器 {node.instance_name or server_name(node.alias)}（区域 {node.region}，只删带 cnsr 标签的）")
    ui.info(f"  — Primary IP {node.static_ip_name or node.alias + STATIC_IP_SUFFIX}")
    ui.info(f"  — 防火墙 {node.alias}{FIREWALL_SUFFIX}")
    return "Hetzner 上的 SSH key（同项目的其他服务器可能还在用）"


def remove_hetzner(node: Node, deps: Deps) -> list[str]:
    """Delete the server, then the Primary IP, then the firewall; return what could not be done.

    The server goes first: that unassigns the Primary IP (deleting it with the server, as cnsr
    creates it) and detaches the firewall. Each name leaves the record once its resource is gone.
    With no recorded server, one named after the alias without cnsr's label is not the node's at
    all. A recorded server the token cannot see counts as gone, unless the saved token's project
    holds it (see ``hetzner_elsewhere``).
    """
    alias = node.alias
    name = node.instance_name or server_name(alias)
    ip_name = node.static_ip_name or f"{alias}{STATIC_IP_SUFFIX}"
    try:
        hetzner = _hetzner(node, deps)
        tagged = hetzner.instance_tagged(name, alias)
        elsewhere = hetzner_elsewhere(deps, node.region, name, alias) if tagged is None else None
    except CnsrError as exc:
        return [str(exc)]
    if elsewhere:
        return [elsewhere]
    if tagged is False and node.instance_name is None:
        tagged = None
    deps.ui.detail("删除 Hetzner 上的服务器、Primary IP 和防火墙。")
    left: list[str] = []
    if tagged is False:
        left.append(f"服务器 {name} 不是 cnsr 创建的（没有标签 {TAG_KEY}={alias}），未删除")
    else:
        errors = hetzner.delete_instance(name) if tagged else []
        if errors:  # its Primary IP and firewall are still attached to it
            return errors
        node.instance_name = None
    errors = hetzner.delete_primary_ip(ip_name, alias)
    if not errors:
        node.static_ip_name = None
    left += errors
    return left + hetzner.delete_firewall(alias)


def _hetzner(node: Node, deps: Deps) -> Hetzner:
    if node.region is None:
        raise CnsrError("节点记录里没有 Hetzner 区域，无法删除 Hetzner 上的资源。")
    hetzner = known_hetzner(deps, node.region)
    if hetzner is None:
        raise CnsrError(
            "没有找到 Hetzner token（环境变量 HCLOUD_TOKEN 和本机保存的都没有），无法删除 Hetzner 上的资源。"
            "请设置好 HCLOUD_TOKEN 后重新运行。"
        )
    return hetzner
