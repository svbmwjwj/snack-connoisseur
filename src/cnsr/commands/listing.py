"""``cnsr list``: the servers in the control plane and how devices subscribe to their nodes.

Read only: nothing is created in Cloudflare, and no server is contacted. The one call out is a
read of each Hetzner server's traffic, with a token already at hand.
"""

from __future__ import annotations

from argparse import Namespace

from rich.cells import cell_len
from rich.text import Text

from cnsr import features, localconf
from cnsr.cloudflare.control import find_control_plane
from cnsr.commands.token_hint import saved_token_hint
from cnsr.aws.lightsail import Lightsail
from cnsr.deploy.context import Deps, default_deps, known_hetzner, known_lightsail
from cnsr.errors import CnsrError
from cnsr.hetzner.cloud import Hetzner
from cnsr.sizes import terabytes
from cnsr.qx import check_tasks, snippet_url, v2rayn_url
from cnsr.state import ControlMeta, Inventory, Node

SOURCES = {"aws": "AWS", "hetzner": "Hetzner", "vps": "自有服务器"}
HEADER = ("别名", "功能", "来源", "域名", "IPv4", "SNI", "流量", "")
MARKS = {features.XRAY: ("● ", "green"), features.HARDEN: ("○ ", "yellow")}  # by the last feature installed
COLUMN_STYLES = ("bold", "", "", "dim", "", "cyan", "", "red")  # alias, domain, SNI and the missing-subscription note
LABEL = "v2rayN 订阅  "  # the widest subscription label and its gap
# How the traffic column reaches each cloud: the client for a region, None without credentials.
KNOWN = {"hetzner": known_hetzner, "aws": known_lightsail}
AWS_NOTE = "AWS 流量为本月入站加出站；同区域同套餐的实例合用额度"


def run(ns: Namespace, deps: Deps | None = None) -> int:
    """Print the servers, and both subscriptions when one runs an Xray node; 1 without a control plane."""
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
        raise CnsrError("还没有服务器（本机还没有 Cloudflare 设置）。")
    found = find_control_plane(deps.make_cf(token), zone)
    if found is None:
        raise CnsrError("还没有服务器（这个 Cloudflare 账号里还没有控制端）。")
    kv, meta, _ = found
    inventory = Inventory(kv)
    nodes = [node for node in map(inventory.get, inventory.aliases()) if node is not None]

    if not nodes:
        ui.info("控制端里还没有服务器。")
        return 0
    ui.styled(Text.assemble(("服务器", "bold"), (f"  {len(nodes)} 台", "dim")))
    show(deps, nodes, meta)
    return 0


def show(deps: Deps, nodes: list[Node], meta: ControlMeta, cloud: Hetzner | Lightsail | None = None) -> None:
    """The servers as a table and, when one runs an Xray node, both subscriptions.

    ``cnsr list`` shows every server so; ``cnsr up`` shows the one it deployed, reading its
    traffic through ``cloud``, the client it deployed with.
    """
    ui = deps.ui
    traffic = _Traffic(deps)
    if cloud is not None and nodes:
        traffic.preset(nodes[0], cloud)
    rows = [_row(node, traffic(node)) for node in nodes]
    for line in _table([HEADER, *rows]):
        ui.styled(Text("  ", no_wrap=True) + line)
    if any(node.source == "aws" and row[6] != "-" for node, row in zip(nodes, rows)):
        ui.styled(Text(f"  {AWS_NOTE}", style="dim", no_wrap=True))
    if not any(features.XRAY in features.installed(node) for node in nodes):
        return
    ui.info("")
    indent = " " * cell_len(LABEL)
    ui.styled(_subscription("圈X 订阅", snippet_url(meta)))
    ui.styled(_subscription("v2rayN 订阅", v2rayn_url(meta)))
    ui.styled(Text(f"{indent}v2rayN 设自动更新，不要勾选通过代理更新", style="dim"))
    ui.info("")
    ui.styled(Text.assemble(("圈X 检测" + " " * (cell_len(LABEL) - cell_len("圈X 检测")), "bold"), "加到圈X 配置的 [task_local]："))
    for line in check_tasks(meta):
        ui.styled(Text(indent + line, style="cyan", no_wrap=True))
    ui.styled(Text(f"{indent}第一行每分钟自动检测；第二行可选，用于在策略页长按节点手动检测", style="dim"))


def _subscription(label: str, url: str) -> Text:
    """A label and an address to copy, printed whole so the terminal never breaks it with a newline."""
    return Text.assemble((label + " " * (cell_len(LABEL) - cell_len(label)), "bold"), (url, "cyan"), no_wrap=True)


class _Traffic:
    """A cloud server's traffic this month against what its plan includes, as "<used> / <included>
    TB": a Hetzner server's outgoing traffic, an AWS instance's inbound plus outgoing. "-" for a
    vendor server, without credentials, or when the call fails.

    One client per cloud and region; nothing is asked, and a failure goes to the log only.
    """

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._clients: dict[tuple[str, str], Hetzner | Lightsail | None] = {}

    def preset(self, node: Node, cloud: Hetzner | Lightsail) -> None:
        """Use ``cloud`` for ``node``'s cloud and region instead of making another client."""
        self._clients[(node.source, node.region)] = cloud

    def __call__(self, node: Node) -> str:
        if node.source not in KNOWN or node.region is None or node.instance_name is None:
            return "-"
        key = (node.source, node.region)
        try:
            if key not in self._clients:
                self._clients[key] = KNOWN[node.source](self._deps, node.region)
            cloud = self._clients[key]
            found = cloud.traffic(node.instance_name) if cloud else None
        except CnsrError as exc:
            self._clients.setdefault(key, None)
            self._deps.ui.detail(f"没能读取 {node.alias} 在 {SOURCES[node.source]} 上的流量：{exc}")
            return "-"
        if found is None:
            return "-"
        used, included = found
        return f"{terabytes(used)} / {terabytes(included)} TB"


def _row(node: Node, traffic: str) -> tuple[str, ...]:
    source = SOURCES.get(node.source, node.source)
    if node.region:
        source += f" {node.region}"
    if node.size:
        source += f" {node.size}"
    installed = features.installed(node)
    mark = MARKS[installed[-1]][0] if installed else "✕ "
    what = mark + (features.summary(installed) if installed else "部署没完成或正在删除")
    sni = node.xray.sni if node.xray and features.XRAY in installed else "-"
    missing = "不在订阅里" if features.XRAY in installed and node.line is None else ""
    return (node.alias, what, source, node.domain or "-", node.ipv4, sni, traffic, missing)


def _table(rows: list[tuple[str, ...]]) -> list[Text]:
    """The rows as lines with aligned columns, the first as a header underlined by a dim rule.

    A CJK character is two cells wide.
    """
    widths = [max(cell_len(row[column]) for row in rows) for column in range(len(rows[0]))]
    marks = {mark: style for mark, style in MARKS.values()}
    lines = []
    for number, row in enumerate(rows):
        line = Text(no_wrap=True)
        for column, (cell, width) in enumerate(zip(row, widths)):
            if column:
                line.append("  ")
            if number == 0:
                style = "dim"
            elif column == 1:
                style = marks.get(cell[:2], "red")
            else:
                style = COLUMN_STYLES[column]
            line.append(cell, style=style)
            line.append(" " * (width - cell_len(cell)))
        line.rstrip()
        lines.append(line)
        if number == 0:
            lines.append(Text("  ".join("─" * width for width in widths if width).rstrip(), style="dim", no_wrap=True))
    return lines
