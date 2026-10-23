"""Tests for ``cnsr list``: the nodes, the Quantumult X sections and the v2rayN address, changing nothing."""

from __future__ import annotations

import argparse

from deploy_fakes import run_add

from cnsr.commands import listing
from cnsr.qx import client_lines, v2rayn_url
from cnsr.state import ControlMeta


def run_list(fakes) -> tuple[int, str]:
    fakes.terminal.clear()
    code = listing.run(argparse.Namespace(command="list", alias=None), fakes.deps)
    return code, fakes.terminal.text


def test_list_shows_the_nodes_and_both_subscriptions(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    meta = ControlMeta.from_json(fake_deps.kv()["meta:control"])
    node = fake_deps.node()
    before = len(fake_deps.cloudflare.calls())

    code, out = run_list(fake_deps)

    assert code == 0
    assert "jp-1" in out and node.domain in out and node.ipv4 in out and node.xray.sni in out
    assert client_lines(meta) in out
    assert v2rayn_url(meta) in out
    assert {method for method, _ in fake_deps.cloudflare.calls()[before:]} == {"GET"}


def test_list_marks_a_node_that_is_not_in_the_subscription(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    node = fake_deps.node()
    node.line = None
    fake_deps.store(node)
    _, out = run_list(fake_deps)
    assert "不在订阅里" in out


def test_list_without_a_control_plane_creates_nothing(fake_deps):
    code, out = run_list(fake_deps)
    assert code == 1
    assert "还没有节点" in out
    assert {method for method, _ in fake_deps.cloudflare.calls()} == {"GET"}


def test_list_without_cloudflare_settings_says_there_are_no_nodes(fake_deps):
    fake_deps.deps.conf_path.unlink()
    code, out = run_list(fake_deps)
    assert code == 1
    assert "还没有节点" in out
    assert fake_deps.calls["make_cf"] == []
