"""``cnsr rm`` without an alias: tick the servers to delete, then confirm once."""

from __future__ import annotations

from deploy_fakes import AGENT, agent_key, run_rm, run_up
from rm_harness import deploy_aws

PROMPT = "要删除哪些服务器？"


def deploy_two(fake_deps) -> None:
    deploy_aws(fake_deps)
    fake_deps.agents[AGENT].append(agent_key("jp-2"))
    fake_deps.lightsail.fresh_addresses = ["203.0.113.60"]
    fake_deps.terminal.inputs = ["1"]  # the static IP menu: a new one, not jp-1's
    assert run_up(fake_deps, "jp-2", aws="jp-tokyo") == 0
    fake_deps.terminal.clear()


def test_every_server_is_listed_unticked_and_the_ticked_ones_are_deleted(fake_deps):
    deploy_two(fake_deps)
    fake_deps.terminal.inputs = ["1,2", "1"]  # tick both, then confirm
    assert run_rm(fake_deps, None) == 0
    screen = fake_deps.terminal.screen
    menu = screen.split(PROMPT + "\n")[1]
    assert menu.startswith("1. jp-1  AWS jp-tokyo 2c0.5g  ") and "\n2. jp-2  AWS jp-tokyo 2c0.5g  " in menu
    assert "（直接回车：）" in fake_deps.terminal.prompts[0]  # none ticked at first
    assert "确认删除这 2 台服务器？" in screen
    assert fake_deps.node("jp-1") is None and fake_deps.node("jp-2") is None


def test_only_the_ticked_server_is_deleted(fake_deps):
    deploy_two(fake_deps)
    fake_deps.terminal.inputs = ["2", "1"]
    assert run_rm(fake_deps, None) == 0
    assert "确认删除服务器 jp-2？" in fake_deps.terminal.screen
    assert fake_deps.node("jp-1") is not None and fake_deps.node("jp-2") is None


def test_ticking_nothing_is_asked_again(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.terminal.inputs = ["", "1", "1"]
    assert run_rm(fake_deps, None) == 0
    assert "至少勾选一台服务器" in fake_deps.terminal.screen


def test_enter_at_the_confirmation_cancels(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.terminal.inputs = ["1", ""]
    assert run_rm(fake_deps, None) == 0
    assert "已取消，没有删除任何东西。" in fake_deps.terminal.screen
    assert "（直接回车：2）" in fake_deps.terminal.prompts[-1]
    assert fake_deps.node() is not None and fake_deps.lightsail.instances


def test_an_alias_given_skips_the_list_and_confirms_once(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps, "jp-1") == 0
    assert PROMPT not in fake_deps.terminal.screen and "输入别名" not in fake_deps.terminal.screen
    assert fake_deps.node() is None


def test_no_server_says_so(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps, "jp-1") == 0
    fake_deps.terminal.clear()
    assert run_rm(fake_deps, None) == 0
    assert "控制端里没有服务器" in fake_deps.terminal.text
