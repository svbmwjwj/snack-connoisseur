"""``cnsr list``: the servers in the control plane and how devices subscribe to their nodes.

Read only: nothing is created in Cloudflare, and no server is contacted.
"""

from __future__ import annotations

from argparse import Namespace

from rich.cells import cell_len
from rich.text import Text

from cnsr import features, localconf
from cnsr.cloudflare.control import find_control_plane
from cnsr.commands.token_hint import saved_token_hint
from cnsr.deploy.context import Deps, default_deps
from cnsr.errors import CnsrError
from cnsr.qx import snippet_url, v2rayn_url
from cnsr.state import Inventory, Node

SOURCES = {"aws": "AWS", "vps": "自有服务器"}
HEADER = ("别名", "功能", "来源", "域名", "IPv4", "SNI", "")
MARKS = {features.XRAY: ("● ", "green"), features.HARDEN: ("○ ", "yellow")}  # by the last feature installed
COLUMN_STYLES = ("bold", "", "", "dim", "", "cyan", "red")  # alias, domain, SNI and the missing-subscription note
LABEL = "v2rayN 订阅  "  # the widest subscription label and its gap


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
    for line in _table([HEADER, *map(_row, nodes)]):
        ui.styled(Text("  ", no_wrap=True) + line)
    if not any(features.XRAY in features.installed(node) for node in nodes):
        return 0
    ui.info("")
    ui.styled(_subscription("圈X 订阅", snippet_url(meta)))
    ui.styled(_subscription("v2rayN 订阅", v2rayn_url(meta)))
    ui.styled(Text(f"{' ' * cell_len(LABEL)}v2rayN 设自动更新，不要勾选通过代理更新", style="dim"))
    return 0


def _subscription(label: str, url: str) -> Text:
    """A label and an address to copy, printed whole so the terminal never breaks it with a newline."""
    return Text.assemble((label + " " * (cell_len(LABEL) - cell_len(label)), "bold"), (url, "cyan"), no_wrap=True)


def _row(node: Node) -> tuple[str, ...]:
    source = SOURCES.get(node.source, node.source)
    if node.region:
        source += f" {node.region}"
    installed = features.installed(node)
    mark = MARKS[installed[-1]][0] if installed else "✕ "
    what = mark + (features.summary(installed) if installed else "部署没完成或正在删除")
    sni = node.xray.sni if node.xray and features.XRAY in installed else "-"
    missing = "不在订阅里" if features.XRAY in installed and node.line is None else ""
    return (node.alias, what, source, node.domain or "-", node.ipv4, sni, missing)


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
