"""``cnsr up`` for a new AWS node that takes over another cnsr node's static IP (a machine swap)."""

from __future__ import annotations

from deploy_fakes import AGENT, agent_key, run_rm, run_up
from test_up_static_ip_menu import NEW, OTHER, add_ip, deploy_other, menu_lines

TAKE_OVER = "1"  # the first answer of the takeover question


def take_over(fake_deps, *answers: str) -> int:
    fake_deps.terminal.inputs = ["2", *answers]  # the menu: 2 = jp-2-ip
    return run_up(fake_deps, aws="jp-tokyo")


def test_the_ip_of_another_cnsr_node_is_offered_as_a_takeover(fake_deps):
    deploy_other(fake_deps)
    assert take_over(fake_deps, TAKE_OVER) == 0
    assert menu_lines(fake_deps) == [f"1. {NEW}", f"2. jp-2-ip  {OTHER}  绑在实例 jp-2 上（接替 cnsr 节点 jp-2）"]


def test_a_takeover_moves_the_ip_and_hands_it_to_the_new_node(fake_deps):
    deploy_other(fake_deps)
    assert take_over(fake_deps, TAKE_OVER) == 0
    question = fake_deps.terminal.seen[1]
    assert "是 cnsr 节点 jp-2 的地址" in question and "jp-2 从那时起断线" in question
    node, old = fake_deps.node(), fake_deps.node("jp-2")
    assert (node.static_ip_name, node.ipv4, node.static_ip_reused, node.static_ip_from) == ("jp-2-ip", OTHER, False, None)
    assert fake_deps.lightsail.ips == {"jp-2-ip": "jp-1"} and "jp-1-ip" not in fake_deps.lightsail.ips
    assert old.static_ip_name is None
    assert "确认 jp-1 可用后运行 cnsr rm jp-2" in fake_deps.terminal.screen


def test_after_a_takeover_rm_of_the_old_node_leaves_the_ip_and_rm_of_the_new_one_releases_it(fake_deps):
    deploy_other(fake_deps)
    assert take_over(fake_deps, TAKE_OVER) == 0
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps, "jp-2") == 0
    assert fake_deps.lightsail.ips == {"jp-2-ip": "jp-1"} and "jp-2" not in fake_deps.lightsail.instances
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps) == 0
    assert fake_deps.lightsail.ips == {}


def test_your_own_ip_taken_over_stays_yours(fake_deps):
    add_ip(fake_deps, "mine", OTHER)
    fake_deps.terminal.inputs = ["2"]  # jp-2 picks your free IP
    fake_deps.agents[AGENT].append(agent_key("jp-2"))
    assert run_up(fake_deps, "jp-2", aws="jp-tokyo") == 0
    fake_deps.terminal.clear()
    assert take_over(fake_deps, TAKE_OVER) == 0
    assert fake_deps.node().static_ip_reused is True
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps) == 0
    assert fake_deps.lightsail.ips == {"mine": None}  # detached and kept


def test_stopping_at_the_question_makes_nothing_and_leaves_the_old_node_alone(fake_deps):
    deploy_other(fake_deps)
    assert take_over(fake_deps, "2") == 1
    assert fake_deps.lightsail.instances == {"jp-2": True} and fake_deps.lightsail.ips == {"jp-2-ip": "jp-2"}
    assert fake_deps.node() is None and fake_deps.node("jp-2").static_ip_name == "jp-2-ip"


def test_a_failed_first_deployment_gives_the_ip_and_the_record_back(fake_deps):
    deploy_other(fake_deps)
    fake_deps.server.service_fails = True
    assert take_over(fake_deps, TAKE_OVER) == 1
    assert fake_deps.lightsail.ips == {"jp-2-ip": "jp-2"}
    assert fake_deps.node("jp-2").static_ip_name == "jp-2-ip"
    assert fake_deps.node() is None


def test_an_ip_left_free_by_the_old_node_is_taken_over_without_a_move(fake_deps):
    deploy_other(fake_deps)
    fake_deps.lightsail.ips["jp-2-ip"] = None  # jp-2's instance was deleted by hand
    assert take_over(fake_deps, TAKE_OVER) == 0
    assert "detach_static_ip" not in fake_deps.lightsail.names()
    assert fake_deps.lightsail.ips["jp-2-ip"] == "jp-1" and fake_deps.node("jp-2").static_ip_name is None


def test_a_given_static_ip_of_another_cnsr_node_still_stops(fake_deps):
    deploy_other(fake_deps)
    assert run_up(fake_deps, aws="jp-tokyo", static_ip=OTHER) == 1
    assert "cnsr 节点 jp-2" in fake_deps.terminal.text
    assert fake_deps.lightsail.ips == {"jp-2-ip": "jp-2"}
