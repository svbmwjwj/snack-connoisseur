"""Region codes: the "country-area" codes on the command line, and older records still read.

Records written before the change hold ``jp``, ``sg`` or ``us``; they are read as the new codes,
so ``rm`` and a rerun of ``up`` still find the node's region. The command line takes only the
new codes and names the new one for an old code.
"""

from __future__ import annotations

import json

import pytest
from deploy_fakes import run_up
from rm_harness import deploy_aws, remove

from cnsr.cli import main, parse_args
from cnsr.state import Node


def make_legacy(fakes, region: str) -> None:
    """Store jp-1's record as a version before the new codes wrote it."""
    raw = json.loads(fakes.kv()["node:jp-1"])
    raw["region"] = region
    fakes.kv()["node:jp-1"] = json.dumps(raw, ensure_ascii=False)


@pytest.mark.parametrize(
    "stored,read",
    [("jp", "jp-tokyo"), ("sg", "sg"), ("us", "us-oregon"), ("jp-tokyo", "jp-tokyo"), ("us-oregon", "us-oregon"), (None, None)],
)
def test_a_stored_region_is_read_as_the_new_code(stored, read):
    raw = json.dumps({"alias": "a", "source": "aws", "ipv4": "192.0.2.1", "region": stored})
    assert Node.from_json(raw).region == read


def test_a_record_without_a_region_still_reads():
    assert Node.from_json(json.dumps({"alias": "a", "source": "vps", "ipv4": "192.0.2.1"})).region is None


def test_rm_deletes_a_node_whose_record_holds_an_old_code(fake_deps):
    deploy_aws(fake_deps)
    make_legacy(fake_deps, "jp")

    code, _ = remove(fake_deps)

    assert code == 0
    assert fake_deps.calls["make_lightsail"] == [("jp-tokyo", None)]
    assert fake_deps.lightsail.instances == {}
    assert fake_deps.node() is None


def test_a_rerun_of_an_old_record_takes_the_new_code(fake_deps):
    deploy_aws(fake_deps)
    make_legacy(fake_deps, "jp")

    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.calls["make_lightsail"] == [("jp-tokyo", None)]
    assert fake_deps.node().region == "jp-tokyo"


def test_a_rerun_of_an_old_record_elsewhere_names_the_new_code(fake_deps):
    deploy_aws(fake_deps)
    make_legacy(fake_deps, "jp")

    assert run_up(fake_deps, aws="sg") == 1
    assert "服务器 jp-1 已存在（AWS jp-tokyo 2c0.5g）" in fake_deps.terminal.text


def test_the_region_menu_shows_the_new_codes_with_their_names(fake_deps):
    fake_deps.terminal.inputs = ["2", "3", ""]  # source: 2 = AWS, region: 3 = us-oregon, size: the default
    assert run_up(fake_deps) == 0
    text = fake_deps.terminal.text
    assert "jp-tokyo（日本东京）" in text and "sg（新加坡）" in text and "us-oregon（美国俄勒冈州）" in text
    assert fake_deps.node().region == "us-oregon"


@pytest.mark.parametrize("code", ["jp-tokyo", "sg", "us-oregon"])
def test_the_command_line_takes_the_new_codes(code):
    assert parse_args(["up", "x", "--aws", code]).aws == code


@pytest.mark.parametrize("old,new", [("jp", "jp-tokyo"), ("us", "us-oregon")])
def test_an_old_code_on_the_command_line_names_the_new_one(old, new, capsys):
    assert main(["up", "x", "--aws", old]) == 2
    err = capsys.readouterr().err
    assert err.startswith("错误：") and f"{old} 已改为 {new}" in err
