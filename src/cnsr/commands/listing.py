"""``cnsr list``: the nodes in the control plane and how devices subscribe to them.

Read only: nothing is created in Cloudflare, and no server is contacted.
"""

from __future__ import annotations

from argparse import Namespace

from cnsr import localconf
from cnsr.cloudflare.control import find_control_plane
from cnsr.commands.token_hint import saved_token_hint
from cnsr.deploy.context import Deps, default_deps
from cnsr.errors import CnsrError
from cnsr.qx import client_lines, v2rayn_url
from cnsr.state import Inventory, Node

SOURCES = {"aws": "AWS", "vps": "自有服务器"}


def run(ns: Namespace, deps: Deps | None = None) -> int:
    """Print the nodes and both subscriptions; 1 when there is no control plane to read."""
    deps = deps or default_deps()
    try:
        with saved_token_hint(deps.conf_path):
            return _list(deps)
    except CnsrError as exc:
        deps.ui.error(str(exc))
        return 1


def _list(deps: Deps) -> int:
    ui = deps.ui
    values = localconf.load(deps.conf_path)
    token, zone = values.get("CLOUDFLARE_API_TOKEN"), values.get("CLOUDFLARE_ZONE")
    if not (token and zone):
        raise CnsrError("还没有节点（本机还没有 Cloudflare 设置）。")
    found = find_control_plane(deps.make_cf(token), zone)
    if found is None:
        raise CnsrError("还没有节点（这个 Cloudflare 账号里还没有控制端）。")
    kv, meta, _ = found
    inventory = Inventory(kv)
    nodes = [node for node in map(inventory.get, inventory.aliases()) if node is not None]

    ui.info(f"节点（{len(nodes)} 个）：" if nodes else "控制端里还没有节点。")
    for node in nodes:
        ui.info(f"  {_describe(node)}")
    ui.info("")
    ui.info("圈X：每台设备在配置里粘贴一次下面几行：")
    ui.info(client_lines(meta))
    ui.info("")
    ui.info("v2rayN 订阅地址（设自动更新，不要勾选通过代理更新）：")
    ui.info(v2rayn_url(meta))
    return 0


def _describe(node: Node) -> str:
    source = SOURCES.get(node.source, node.source)
    if node.region:
        source += f" {node.region}"
    address = f"{node.domain}（{node.ipv4}）" if node.domain else node.ipv4
    sni = f"，SNI {node.xray.sni}" if node.xray else ""
    missing = "，不在订阅里（部署没完成或正在删除）" if node.line is None else ""
    return f"{node.alias}：{source}，{address}{sni}{missing}"
