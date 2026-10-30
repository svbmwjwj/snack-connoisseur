"""``cnsr list`` with Hetzner servers: the size in the source column and this period's traffic.

The traffic is one read-only Hetzner call per region; without a token, or when the call fails,
the column shows "-" and the rest of the table is unchanged.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest
from deploy_fakes import run_up
from hetzner_fakes import TB

from cnsr import localconf
from cnsr.commands import listing
from cnsr.errors import CnsrError

HETZNER_TOKEN = "hz-env-token-0001"


@pytest.fixture
def hz(fake_deps):
    fake_deps.hcloud_env = HETZNER_TOKEN
    fake_deps.terminal.inputs = ["已保存"]
    assert run_up(fake_deps, hetzner="sg", size="1c1g") == 0
    fake_deps.hetzner.calls.clear()
    fake_deps.calls.clear()
    return fake_deps


def run_list(fakes) -> tuple[int, str]:
    fakes.terminal.clear()
    code = listing.run(argparse.Namespace(command="list", alias=None), fakes.deps)
    return code, fakes.terminal.text


def add_node(fakes, alias: str, **fields) -> None:
    """Store a copy of jp-1 under ``alias`` with ``fields`` changed, and add it to the index."""
    node = fakes.node()
    node.alias, node.line = alias, None
    for name, value in fields.items():
        setattr(node, name, value)
    fakes.store(node)
    aliases = json.loads(fakes.kv()["index:nodes"])
    fakes.kv()["index:nodes"] = json.dumps([*aliases, alias])


def row(out: str, alias: str) -> str:
    return next(line for line in out.splitlines() if re.match(rf"\s*{re.escape(alias)} ", line))


def test_the_source_names_the_vendor_region_and_size(hz):
    _, out = run_list(hz)
    assert "Hetzner sg 1c1g" in row(out, "jp-1")


def test_the_traffic_column_shows_this_periods_outgoing_and_included_traffic(hz):
    hz.hetzner.used["jp-1"] = int(1.2 * TB)
    code, out = run_list(hz)
    assert code == 0
    header = next(line for line in out.splitlines() if "别名" in line)
    assert "流量" in header
    assert row(out, "jp-1").rstrip().endswith("1.2 / 20 TB")
    assert hz.hetzner.calls == [("traffic", "jp-1")]
    assert hz.calls["make_hetzner"] == [("sg", HETZNER_TOKEN)]


def test_little_traffic_reads_as_zero(hz):
    _, out = run_list(hz)
    assert "0 / 20 TB" in row(out, "jp-1")


def test_the_saved_token_is_used_when_the_environment_has_none(hz):
    hz.hcloud_env = None
    values = localconf.load(hz.deps.conf_path)
    localconf.save(hz.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token-0002"})
    _, out = run_list(hz)
    assert "0 / 20 TB" in row(out, "jp-1")
    assert hz.calls["make_hetzner"] == [("sg", "hz-saved-token-0002")]


def test_without_a_token_the_traffic_is_a_dash_and_nothing_is_asked(hz):
    hz.hcloud_env = None
    code, out = run_list(hz)
    assert code == 0
    assert row(out, "jp-1").rstrip().endswith("-") and "TB" not in out
    assert hz.calls["make_hetzner"] == [] and hz.hetzner.calls == []
    assert "Hetzner sg 1c1g" in row(out, "jp-1")


def test_a_failed_call_is_a_dash_not_an_error(hz):
    def fail(name):
        raise CnsrError("无法连接 Hetzner API：连接被拒绝")

    hz.hetzner.traffic = fail
    code, out = run_list(hz)
    assert code == 0
    assert row(out, "jp-1").rstrip().endswith("-")
    assert "连接被拒绝" not in hz.terminal.screen


def test_a_client_that_cannot_be_made_is_a_dash(hz):
    def refuse(region, token):
        raise CnsrError("Hetzner 没有区域 jp-tokyo")

    hz.deps.make_hetzner = refuse
    code, out = run_list(hz)
    assert code == 0 and row(out, "jp-1").rstrip().endswith("-")


def test_a_server_that_is_gone_is_a_dash(hz):
    hz.hetzner.servers.clear()
    _, out = run_list(hz)
    assert row(out, "jp-1").rstrip().endswith("-")


def test_one_client_per_region(hz):
    add_node(hz, "jp-2", instance_name="jp-2")
    add_node(hz, "de-1", instance_name="de-1", region="de-falkenstein", size="2c4g")
    hz.hetzner.servers.update({"jp-2": True, "de-1": True})
    _, out = run_list(hz)
    assert sorted(hz.calls["make_hetzner"]) == [("de-falkenstein", HETZNER_TOKEN), ("sg", HETZNER_TOKEN)]
    assert sorted(call[1] for call in hz.hetzner.calls) == ["de-1", "jp-1", "jp-2"]
    assert "Hetzner de-falkenstein 2c4g" in row(out, "de-1")


def test_other_servers_show_a_dash_and_contact_no_one(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.hcloud_env = HETZNER_TOKEN
    fake_deps.lightsail.calls.clear()
    code, out = run_list(fake_deps)
    assert code == 0
    line = row(out, "jp-1")
    assert "AWS jp-tokyo" in line and line.rstrip().endswith("-")
    assert fake_deps.calls["make_hetzner"] == [] and fake_deps.lightsail.calls == []


def test_an_old_record_without_a_size_shows_the_region_only(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    _, out = run_list(fake_deps)
    assert re.search(r"AWS jp-tokyo +", row(out, "jp-1"))


def test_list_stays_read_only(hz):
    before = len(hz.cloudflare.calls())
    run_list(hz)
    assert {method for method, _ in hz.cloudflare.calls()[before:]} == {"GET"}
    assert [call[0] for call in hz.hetzner.calls] == ["traffic"]


def test_the_token_never_shows(hz):
    run_list(hz)
    assert HETZNER_TOKEN not in hz.terminal.text
