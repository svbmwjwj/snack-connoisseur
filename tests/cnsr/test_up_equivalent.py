"""``cnsr up --to base|xray``, and the command line ``cnsr up`` prints for a new server."""

from __future__ import annotations

import pytest
from deploy_fakes import VPS_IP, preflight_output, run_up

from cnsr import features
from cnsr.cli import main, parse_args

HARDEN_ONLY = [features.HARDEN]
BOTH = list(features.ALL)
VPS_ANSWERS = ["", "", "已保存"]  # SSH user and port (the defaults), the console password
KEPT = "203.0.113.50"
PREFIX = "等价命令（换一个别名即可按同样的配置再开一台）："


def equivalent(fake_deps) -> list[str]:
    """The equivalent commands on the screen, without the note in front."""
    lines = fake_deps.terminal.screen.splitlines()
    return [line.removeprefix(PREFIX) for line in lines if line.startswith(PREFIX)]


# --- the equivalent command ------------------------------------------------------------------


def test_a_new_aws_server_prints_its_command(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to xray"]


def test_a_region_picked_in_the_menus_is_written_out(fake_deps):
    fake_deps.terminal.inputs = ["2", "3", ""]  # source: 2 = AWS, region: 3 = us-oregon, size: the default
    assert run_up(fake_deps) == 0
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws us-oregon --size 2c0.5g --to xray"]


def test_a_static_ip_is_left_out(fake_deps):
    """A static IP serves one node only: another alias with the same --static-ip would take it."""
    fake_deps.lightsail.ips["spare"] = None
    fake_deps.lightsail.addresses["spare"] = KEPT
    assert run_up(fake_deps, aws="sg", static_ip=KEPT) == 0
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws sg --size 2c0.5g --to xray"]


def test_a_vendor_server_prints_its_address(fake_deps):
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert equivalent(fake_deps) == [f"cnsr up jp-1 --ip {VPS_IP} --to xray"]


def test_an_address_typed_in_the_questions_is_written_out(fake_deps):
    fake_deps.terminal.inputs = ["1", VPS_IP, *VPS_ANSWERS]  # source: 1 = an existing server
    assert run_up(fake_deps) == 0
    assert equivalent(fake_deps) == [f"cnsr up jp-1 --ip {VPS_IP} --to xray"]


def test_an_alias_typed_in_the_questions_is_written_out(fake_deps):
    fake_deps.terminal.inputs = ["jp-1"]  # the alias
    assert run_up(fake_deps, None, aws="sg") == 0
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws sg --size 2c0.5g --to xray"]


def test_the_hardening_alone_is_written_as_base(fake_deps):
    fake_deps.terminal.inputs = ["1"]  # only the base ticked
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to base"]


def test_no_secret_is_in_the_command(fake_deps):
    fake_deps.aws_chain = False
    key_id, secret = "AKIAEXAMPLE0001", "wJalrXUtnFEMI-example-secret"
    fake_deps.terminal.inputs = [key_id, secret]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    (line,) = equivalent(fake_deps)
    assert line == "cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to xray"
    for value in (key_id, secret, "t-secret"):
        assert value not in fake_deps.terminal.screen and value not in fake_deps.terminal.log


def test_it_comes_after_the_questions_and_before_step_one(fake_deps):
    fake_deps.terminal.inputs = ["2", "1", "", ""]  # source: AWS, region: jp-tokyo, features: both, size: default
    assert run_up(fake_deps, features=None) == 0
    screen = fake_deps.terminal.screen
    assert screen.index("要部署哪些功能？") < screen.index(PREFIX) < screen.index("[1/")


def test_a_run_that_fails_has_printed_it_already(fake_deps):
    fake_deps.server.preflight = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"), user="admin")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert equivalent(fake_deps) == ["cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to xray"]


def test_a_rerun_prints_no_command(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=HARDEN_ONLY) == 0
    fake_deps.terminal.clear()
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert equivalent(fake_deps) == []


# --- --to ------------------------------------------------------------------------------------


@pytest.mark.parametrize("to, chosen", [("base", HARDEN_ONLY), ("xray", BOTH)])
def test_to_deploys_up_to_that_feature_without_the_menu(fake_deps, to, chosen):
    assert run_up(fake_deps, aws="jp-tokyo", features=None, to=to) == 0  # no answer queued
    assert "要部署哪些功能？" not in fake_deps.terminal.text
    assert fake_deps.node().features == chosen
    assert equivalent(fake_deps) == [f"cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to {to}"]


def test_to_base_keeps_what_a_server_has_already(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert run_up(fake_deps, aws="jp-tokyo", features=None, to="base") == 0
    assert fake_deps.node().features == BOTH


def test_to_xray_adds_xray_to_a_hardened_server(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=HARDEN_ONLY) == 0
    assert run_up(fake_deps, aws="jp-tokyo", features=None, to="xray") == 0
    assert fake_deps.node().features == BOTH


@pytest.mark.parametrize("to", ["base", "xray"])
def test_the_command_line_takes_to(to):
    assert parse_args(["up", "x", "--aws", "sg", "--to", to]).to == to


def test_without_to_the_menu_decides():
    assert parse_args(["up", "x"]).to is None


@pytest.mark.parametrize("argv", [
    ["up", "x", "--to", "harden"],
    ["up", "x", "--to", ""],
    ["up", "x", "--to"],
    ["up", "x", "--to", "base,xray"],
])
def test_a_wrong_to_is_reported_in_chinese(argv, capsys):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("错误：") and "--to 只能是 base 或 xray" in err


def test_the_command_line_and_up_know_the_same_values():
    from cnsr.cli import TO_FEATURES
    from cnsr.commands.up import TO_FEATURES as NAMES

    assert TO_FEATURES == tuple(NAMES) and tuple(NAMES.values()) == features.ALL
