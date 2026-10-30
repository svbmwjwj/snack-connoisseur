"""``cnsr up --hetzner <region> --size <size>`` on the command line, and the record's new field."""

from __future__ import annotations

import json

import pytest

from cnsr import cli
from cnsr.cli import main, parse_args
from cnsr.hetzner import cloud
from cnsr.state import CLOUD_SOURCES, Node, is_cloud


def test_the_tables_match_the_hetzner_modules():
    assert cli.HETZNER_REGIONS == tuple(cloud.REGIONS)


@pytest.mark.parametrize("code", cli.HETZNER_REGIONS)
def test_every_hetzner_region_parses(code):
    ns = parse_args(["up", "x", "--hetzner", code, "--size", "2c4g"])
    assert (ns.hetzner, ns.size, ns.aws, ns.ip) == (code, "2c4g", None, None)


def test_both_default_to_none():
    ns = parse_args(["up", "x"])
    assert ns.hetzner is None and ns.size is None


@pytest.mark.parametrize("argv,expected", [
    (["up", "x", "--hetzner", "jp-tokyo"], "--hetzner 的区域只能是 sg、us-oregon、us-virginia、de-falkenstein、de-nuremberg、fi-helsinki"),
    (["up", "x", "--hetzner", "fsn1"], "--hetzner 的区域只能是 sg、"),
    (["up", "x", "--hetzner"], "--hetzner 的区域只能是"),
    (["up", "x", "--hetzner", "sg", "--size"], "--size 要写规格，例如 2c4g"),
    (["up", "x", "--hetzner", "sg", "--aws", "sg"], "--aws 与 --hetzner 不能同时使用"),
    (["up", "x", "--aws", "sg", "--hetzner", "sg"], "--aws 与 --hetzner 不能同时使用"),
    (["up", "x", "--ip", "192.0.2.1", "--hetzner", "sg"], "--hetzner 与 --ip 不能同时使用"),
    (["up", "x", "--ip", "192.0.2.1", "--aws", "sg"], "--aws 与 --ip 不能同时使用"),
])  # fmt: skip
def test_usage_errors_are_reported_in_chinese(argv, expected, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("错误：") and expected in err
    assert "argument" not in err


def test_the_help_names_both_options(capsys):
    with pytest.raises(SystemExit):
        parse_args(["-h"])
    out = capsys.readouterr().out
    assert "--hetzner <区域>" in out and "--size <规格>" in out
    assert "de-falkenstein" in out and "配合 --hetzner：机型型号" in out


def test_a_record_without_a_size_still_reads():
    raw = json.dumps({"alias": "jp-1", "source": "aws", "ipv4": "192.0.2.1", "region": "sg"})
    assert Node.from_json(raw).size is None


def test_a_hetzner_record_keeps_its_size():
    node = Node(alias="hz-1", source="hetzner", ipv4="192.0.2.1", region="de-falkenstein", size="1c1g")
    assert Node.from_json(node.to_json()) == node


@pytest.mark.parametrize("source,cloud_server", [("aws", True), ("hetzner", True), ("vps", False)])
def test_is_cloud(source, cloud_server):
    assert is_cloud(source) is cloud_server
    assert CLOUD_SOURCES == ("aws", "hetzner")
