"""Which servers ``cnsr rm`` deletes: the one named, or those ticked in a list of every server."""

from __future__ import annotations

from cnsr import features, localconf
from cnsr.cloudflare.api import CloudflareClient
from cnsr.cloudflare.control import find_control_plane
from cnsr.commands.listing import SOURCES
from cnsr.deploy.context import Deps
from cnsr.errors import CnsrError
from cnsr.state import Inventory, Node
from cnsr.ui import Ui

PROMPT = "要删除哪些服务器？"
NONE_TICKED = "至少勾选一台服务器；不删就按 Ctrl-C 退出。"


def choose(ui: Ui, inventory: Inventory) -> list[Node]:
    """The servers ticked, in alias order; none is ticked at first. Empty when there is none."""
    nodes = [node for node in map(inventory.get, inventory.aliases()) if node is not None]
    if not nodes:
        ui.info("控制端里没有服务器，没有可删除的。")
        return []
    ticks = ui.choose_many(PROMPT, [_row(node) for node in nodes], set(), set(), lambda picked: None if picked else NONE_TICKED)
    return [nodes[index] for index in ticks]


def _row(node: Node) -> str:
    """The alias, where the server is, its address and what it runs, as tab-separated columns."""
    where = " ".join(part for part in (SOURCES.get(node.source, node.source), node.region, node.size) if part)
    installed = features.installed(node)
    what = features.summary(installed) if installed else "部署没完成"
    return "\t".join((node.alias, where, node.domain or node.ipv4 or "-", what))


def find_control(alias: str | None, deps: Deps) -> tuple[CloudflareClient, str, Inventory]:
    """The control plane's records and what is needed to delete the rest; creates nothing in Cloudflare."""
    what = f"没有名为 {alias} 的服务器" if alias else "没有可删除的服务器"
    values = localconf.load(deps.conf_path)
    token, zone = values.get("CLOUDFLARE_API_TOKEN"), values.get("CLOUDFLARE_ZONE")
    if not (token and zone):
        raise CnsrError(f"{what}（本机还没有 Cloudflare 设置）。")
    cf = deps.make_cf(token)
    found = find_control_plane(cf, zone)
    if found is None:
        raise CnsrError(f"{what}（这个 Cloudflare 账号里还没有控制端）。")
    kv, _, zone_id = found
    return cf, zone_id, Inventory(kv)


def find_node(alias: str, inventory: Inventory) -> Node:
    node = inventory.get(alias)
    if node is None:
        raise CnsrError(f"没有名为 {alias} 的服务器。")
    return node
