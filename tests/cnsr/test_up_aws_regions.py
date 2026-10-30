"""``cnsr up`` on AWS: the region menu (three featured regions, then more) and every Lightsail region."""

from __future__ import annotations

import pytest
from deploy_fakes import run_up

from cnsr import cli
from cnsr.aws import lightsail
from cnsr.cli import parse_args

MENU = "AWS 区域"


def menu_lines(screen: str, prompt: str = MENU) -> list[str]:
    lines = screen.splitlines()
    start = lines.index(prompt) + 1
    options = []
    for line in lines[start:]:
        if not line[:1].isdecimal():
            break
        options.append(line)
    return options


def test_the_menu_shows_three_regions_then_more(fake_deps):
    fake_deps.terminal.inputs = ["2", "1", ""]  # source: AWS, region: jp-tokyo, size: the default
    assert run_up(fake_deps) == 0
    assert menu_lines(fake_deps.terminal.screen) == [
        "1. jp-tokyo（日本东京）", "2. sg（新加坡）", "3. us-oregon（美国俄勒冈州）", "4. 更多地区…",
    ]  # fmt: skip
    assert fake_deps.node().region == "jp-tokyo"


def test_more_lists_every_region_and_the_pick_is_used(fake_deps):
    codes = list(lightsail.REGIONS)
    frankfurt = codes.index("de-frankfurt") + 1
    fake_deps.terminal.inputs = ["2", "4", str(frankfurt), ""]
    assert run_up(fake_deps) == 0
    screen = fake_deps.terminal.screen
    assert screen.count(f"{MENU}\n") == 2
    listed = menu_lines(screen.split("4. 更多地区…", 1)[1])
    assert len(listed) == len(codes) == 19
    assert f"{frankfurt}. de-frankfurt（德国法兰克福）" in listed
    assert fake_deps.calls["make_lightsail"][0][0] == "de-frankfurt" and fake_deps.node().region == "de-frankfurt"


def test_a_region_to_enable_first_says_so(fake_deps):
    codes = list(lightsail.REGIONS)
    fake_deps.terminal.inputs = ["2", "4", "1", ""]
    assert run_up(fake_deps) == 0
    for code in ("hk", "id-jakarta", "my", "es"):
        number = codes.index(code) + 1
        assert f"{number}. {code}（{lightsail.REGION_NAMES[code]}，需先在 AWS 账户里启用）" in fake_deps.terminal.screen


def test_every_region_maps_to_a_lightsail_region_with_a_name():
    assert len(lightsail.REGIONS) == 19 and set(lightsail.REGION_NAMES) == set(lightsail.REGIONS)
    assert lightsail.REGIONS["de-frankfurt"] == "eu-central-1" and lightsail.REGIONS["us-virginia"] == "us-east-1"
    assert set(lightsail.FEATURED_REGIONS) == {"jp-tokyo", "sg", "us-oregon"}
    assert lightsail.OPT_IN <= set(lightsail.REGIONS)
    assert lightsail.Lightsail("uk-london", client=object()).region == "eu-west-2"


def test_the_command_line_table_matches_the_lightsail_module():
    assert cli.AWS_REGIONS == tuple(lightsail.REGIONS)


@pytest.mark.parametrize("code", ["de-frankfurt", "us-virginia", "hk", "br-sao-paulo"])
def test_the_command_line_takes_every_region(code):
    assert parse_args(["up", "x", "--aws", code]).aws == code


def test_back_in_more_regions_returns_to_the_three(fake_deps):
    fake_deps.terminal.inputs = ["2", "4", "0", "2", ""]  # more, back, then sg
    assert run_up(fake_deps) == 0
    assert fake_deps.terminal.screen.count("4. 更多地区…") == 2
    assert fake_deps.node().region == "sg"


def test_back_from_the_region_returns_to_the_source(fake_deps):
    fake_deps.terminal.inputs = ["2", "0", "2", "2", ""]  # AWS, back, AWS again, sg, size
    assert run_up(fake_deps) == 0
    assert fake_deps.terminal.screen.count("服务器来源\n") == 2 and fake_deps.node().region == "sg"
