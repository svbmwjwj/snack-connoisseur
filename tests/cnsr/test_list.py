"""Tests for ``cnsr list``: the nodes, the Quantumult X sections and the v2rayN address, changing nothing."""

from __future__ import annotations

import argparse
import io
import json
import re

from rich.cells import cell_len
from rich.console import Console

from deploy_fakes import run_up

from cnsr.commands import listing
from cnsr.progress import LiveStages
from cnsr.qx import check_url, snippet_url, v2rayn_url
from cnsr.state import ControlMeta
from cnsr.ui import Ui


def run_list(fakes) -> tuple[int, str]:
    fakes.terminal.clear()
    code = listing.run(argparse.Namespace(command="list", alias=None), fakes.deps)
    return code, fakes.terminal.text


def test_list_shows_the_nodes_and_both_subscriptions(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    meta = ControlMeta.from_json(fake_deps.kv()["meta:control"])
    node = fake_deps.node()
    before = len(fake_deps.cloudflare.calls())

    code, out = run_list(fake_deps)

    assert code == 0
    assert "jp-1" in out and node.domain in out and node.ipv4 in out and node.xray.sni in out
    assert snippet_url(meta) in out and "[server_remote]" not in out
    assert v2rayn_url(meta) in out
    assert {method for method, _ in fake_deps.cloudflare.calls()[before:]} == {"GET"}


def test_list_marks_a_node_that_is_not_in_the_subscription(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    node = fake_deps.node()
    node.line = None
    fake_deps.store(node)
    _, out = run_list(fake_deps)
    assert "不在订阅里" in out


def test_list_without_a_control_plane_creates_nothing(fake_deps):
    code, out = run_list(fake_deps)
    assert code == 1
    assert "还没有服务器" in out
    assert {method for method, _ in fake_deps.cloudflare.calls()} == {"GET"}


def test_list_without_cloudflare_settings_says_there_are_no_nodes(fake_deps):
    fake_deps.deps.conf_path.unlink()
    code, out = run_list(fake_deps)
    assert code == 1
    assert "还没有服务器" in out
    assert fake_deps.calls["make_cf"] == []


def test_list_names_what_each_server_has(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    hardened = fake_deps.node()
    hardened.alias, hardened.features, hardened.xray, hardened.line = "jp-2", ["harden"], None, None
    fake_deps.store(hardened)
    fake_deps.kv()["index:nodes"] = json.dumps(["jp-1", "jp-2"])
    _, out = run_list(fake_deps)
    assert "服务器  2 台" in out
    assert re.search(r"jp-1 +● Xray 节点 ", out) and re.search(r"jp-2 +○ 仅加固 ", out)


def test_list_without_an_xray_node_shows_no_subscription(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=["harden"]) == 0
    meta = ControlMeta.from_json(fake_deps.kv()["meta:control"])
    _, out = run_list(fake_deps)
    assert re.search(r"jp-1 +○ 仅加固 ", out)
    assert v2rayn_url(meta) not in out and "圈X" not in out


def test_list_aligns_the_columns(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    hardened = fake_deps.node()
    hardened.alias, hardened.features, hardened.xray, hardened.line = "jp-22", ["harden"], None, None
    fake_deps.store(hardened)
    fake_deps.kv()["index:nodes"] = json.dumps(["jp-1", "jp-22"])
    _, out = run_list(fake_deps)
    lines = out.splitlines()
    first = next(number for number, line in enumerate(lines) if line.startswith("服务器  2 台"))
    rows = [lines[first + 1], *lines[first + 3 :][:2]]  # the header, then the servers below its rule
    assert len(rows) == 3 and rows[0].split()[:2] == ["别名", "功能"]
    starts = {cell_len(row[: row.index(word)]) for row, word in zip(rows, ["来源", "AWS", "AWS"])}
    assert len(starts) == 1


def test_list_in_a_terminal_colours_the_table_and_never_breaks_an_address(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    meta = ControlMeta.from_json(fake_deps.kv()["meta:control"])
    screen = io.StringIO()
    console = Console(file=screen, force_terminal=True, width=60, color_system="standard")
    fake_deps.deps.ui = Ui(out=screen, interactive=False, display=LiveStages(console, auto_refresh=False))
    assert listing.run(argparse.Namespace(command="list", alias=None), fake_deps.deps) == 0
    out = screen.getvalue()
    assert "\x1b[32m● Xray 节点" in out  # green
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    for url in (snippet_url(meta), v2rayn_url(meta)):
        assert any(line.endswith(url) for line in plain.splitlines())


def test_list_shows_the_quantumult_x_check_tasks_ready_to_paste(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    meta = ControlMeta.from_json(fake_deps.kv()["meta:control"])
    _, out = run_list(fake_deps)
    assert "圈X 检测" in out and "[task_local]" in out
    assert f"* * * * * {check_url(meta)}, tag=cnsr 节点检测, enabled=true" in out
    ui_url = check_url(meta).replace("check.js", "check-ui.js")
    assert f"event-interaction {ui_url}, tag=cnsr 检测, enabled=true" in out
    assert "长按节点手动检测" in out


def test_list_shows_aws_traffic_in_and_out_with_a_note_on_the_shared_allowance(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.lightsail.used = {"jp-1": (300 * 1024**3, 1024**4)}
    _, out = run_list(fake_deps)
    row = next(line for line in out.splitlines() if line.strip().startswith("jp-1 "))
    assert "0.3 / 1 TB" in row
    assert "AWS 流量为本月入站加出站" in out and "合用额度" in out


def test_aws_traffic_that_cannot_be_read_is_a_dash(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.lightsail.instances.clear()  # gone: no traffic to read
    _, out = run_list(fake_deps)
    row = next(line for line in out.splitlines() if line.strip().startswith("jp-1 "))
    assert " - " in row or row.rstrip().endswith("-")
