"""``cnsr up --aws``: the size menu (three featured sizes, then more), ``--size`` and reruns."""

from __future__ import annotations

import pytest
from deploy_fakes import SIZE_MENU, run_rm, run_up
from lightsail_fakes import bundle

from cnsr.sizes import describe, every, parse, pick
from test_up_equivalent import equivalent

PROMPT = "AWS 规格（月价含 IPv4 地址）"


def test_the_menu_shows_three_sizes_then_more_and_enter_takes_nano(fake_deps):
    fake_deps.terminal.inputs = [""]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    screen = fake_deps.terminal.screen
    assert PROMPT in screen
    assert "1. 2c0.5g  nano_3_0   2 核  0.5 GB 内存  20 GB 磁盘  含 1 TB 流量，超出另计   5 美元/月" in screen
    assert "2. 2c1g    micro_3_0  2 核    1 GB 内存  40 GB 磁盘  含 1 TB 流量，超出另计   7 美元/月" in screen
    assert "3. 2c2g    small_3_0  2 核    2 GB 内存  60 GB 磁盘  含 1 TB 流量，超出另计  12 美元/月" in screen
    assert "4. 更多规格…" in screen
    assert "（直接回车：1）" in fake_deps.terminal.prompts[0]
    assert fake_deps.lightsail.created == {"jp-1": "nano_3_0"} and fake_deps.node().size == "2c0.5g"


def test_a_featured_size_picked_is_bought_recorded_and_written_out(fake_deps):
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert fake_deps.lightsail.created == {"jp-1": "micro_3_0"} and fake_deps.node().size == "2c1g"
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws sg --size 2c1g --to xray"]


def test_more_lists_every_size_and_the_pick_is_bought(fake_deps):
    fake_deps.terminal.inputs = ["4", "5"]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    screen = fake_deps.terminal.screen
    assert "5. 2c8g    large_3_0   2 核    8 GB 内存  160 GB 磁盘  含 1 TB 流量，超出另计  44 美元/月" in screen
    assert fake_deps.lightsail.created == {"jp-1": "large_3_0"} and fake_deps.node().size == "2c8g"


def test_a_featured_size_not_sold_is_listed_but_cannot_be_picked(fake_deps):
    fake_deps.lightsail.bundles = [bundle("nano_3_0", 2, 0.5, "5"), bundle("micro_3_0", 2, 1, "7")]
    fake_deps.terminal.inputs = ["3", "2"]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert "3. 2c2g  至少 2 核 / 2 GB 内存（该区域暂无货）" in fake_deps.terminal.screen
    assert fake_deps.node().size == "2c1g"


def test_size_shows_the_line_without_a_menu(fake_deps):
    assert run_up(fake_deps, aws="sg", size="1c1g") == 0
    offer = pick(fake_deps.lightsail.bundles, "1c1g")
    assert f"AWS 规格：{describe(offer)}" in fake_deps.terminal.out.getvalue()
    assert fake_deps.lightsail.created == {"jp-1": "micro_3_0"} and fake_deps.node().size == "1c1g"


def test_without_a_terminal_the_default_bundle_is_bought(fake_deps):
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert fake_deps.lightsail.created == {"jp-1": "nano_3_0"} and fake_deps.node().size == "2c0.5g"


def test_a_size_that_is_no_size_stops_before_anything(fake_deps):
    assert run_up(fake_deps, aws="sg", size="big") == 1
    assert "--size 的 big 不是规格" in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == []


def test_a_rerun_keeps_the_size_unasked(fake_deps):
    fake_deps.terminal.inputs = ["3"]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0  # no answer queued
    assert fake_deps.node().size == "2c2g"


def test_an_instance_gone_comes_back_as_the_recorded_size(fake_deps):
    assert run_up(fake_deps, aws="sg", size="2c2g") == 0
    fake_deps.lightsail.instances.clear()
    fake_deps.lightsail.created.clear()
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0  # no answer queued
    assert fake_deps.lightsail.created == {"jp-1": "small_3_0"}


def test_an_old_record_without_a_size_is_the_default_bundle(fake_deps):
    assert run_up(fake_deps, aws="sg") == 0
    node = fake_deps.node()
    node.size = None
    fake_deps.store(node)
    fake_deps.lightsail.instances.clear()
    fake_deps.lightsail.created.clear()
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert fake_deps.lightsail.created == {"jp-1": "nano_3_0"}
    assert run_up(fake_deps, aws="sg", size="2c1g") == 1
    assert "已存在（AWS sg 2c0.5g）" in fake_deps.terminal.text


@pytest.mark.parametrize("size,cores,memory", [("2c0.5g", 2, 0.5), ("16c64g", 16, 64)])
def test_parse_reads_cores_and_memory(size, cores, memory):
    assert parse(size) == (cores, memory)


@pytest.mark.parametrize("size", ["2c", "c1g", "2C1G", "1.5c1g", "", "2c1gb"])
def test_parse_refuses_anything_else(size):
    assert parse(size) is None


def test_every_leaves_out_a_bundle_a_cheaper_one_beats():
    catalog = [bundle("cheap", 2, 2, "5"), bundle("dear", 2, 1, "9")]
    assert [offer.name for offer in every(catalog)] == ["cheap"]


def test_pick_of_no_size_sold_is_none():
    assert pick([bundle("nano", 2, 0.5, "5")], "4c8g") is None


def test_back_in_more_sizes_returns_to_the_three(fake_deps):
    fake_deps.terminal.inputs = ["4", "0", "2"]  # more, back, then 2c1g
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    assert "（0 返回）" in fake_deps.terminal.prompts[1]
    assert fake_deps.terminal.screen.count("4. 更多规格…") == 2
    assert fake_deps.node().size == "2c1g"


def test_the_size_lines_line_up_in_columns(fake_deps):
    fake_deps.terminal.inputs = [""]
    assert run_up(fake_deps, aws="sg", size=SIZE_MENU) == 0
    lines = [line for line in fake_deps.terminal.screen.splitlines() if line[:3] in ("1. ", "2. ", "3. ")][:3]
    assert len({line.index("美元") for line in lines}) == 1 and len({line.index("GB 内存") for line in lines}) == 1


def test_a_name_another_resource_holds_gives_the_instance_a_number_and_rm_finds_it(fake_deps):
    fake_deps.lightsail.taken = {"jp-1"}  # a key pair of the alias's name, say
    assert run_up(fake_deps, aws="sg") == 0
    node = fake_deps.node()
    assert node.instance_name == "jp-1-1" and fake_deps.lightsail.created == {"jp-1-1": "nano_3_0"}
    assert fake_deps.lightsail.ips["jp-1-ip"] == "jp-1-1"
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps) == 0
    assert "jp-1-1" not in fake_deps.lightsail.instances and fake_deps.lightsail.ips == {}


def test_a_rerun_keeps_the_numbered_name(fake_deps):
    fake_deps.lightsail.taken = {"jp-1", "jp-1-1"}
    assert run_up(fake_deps, aws="sg") == 0
    assert run_up(fake_deps, aws="sg") == 0
    assert fake_deps.node().instance_name == "jp-1-2" and list(fake_deps.lightsail.instances) == ["jp-1-2"]
