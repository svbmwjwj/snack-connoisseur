"""``cnsr up`` for a new AWS node, with a terminal: a menu of the region's static IPs picks its address."""

from __future__ import annotations

import pytest
from deploy_fakes import AGENT, VPS_IP, agent_key, run_rm, run_up

from cnsr.state import Inventory

MENU = "节点地址（AWS 静态 IP）"
NEW = "新申请一个静态 IP（cnsr rm 时释放）"
OTHER = "203.0.113.60"  # the address of jp-2's own static IP


def add_ip(fake_deps, name: str, address: str, attached_to: str | None = None) -> None:
    fake_deps.lightsail.ips[name] = attached_to
    fake_deps.lightsail.addresses[name] = address


def menu_lines(fake_deps) -> list[str]:
    """The numbered options of the static IP menu, as the screen shows them."""
    lines = fake_deps.terminal.screen.splitlines()
    start = lines.index(MENU) + 1
    options = []
    for line in lines[start:]:
        if not line[:1].isdecimal():
            break
        options.append(line)
    return options


def asked(fake_deps) -> bool:
    return MENU in fake_deps.terminal.screen


def deploy_other(fake_deps) -> None:
    """Deploy the cnsr node jp-2 (static IP jp-2-ip at ``OTHER``), then forget the screen."""
    fake_deps.agents[AGENT].append(agent_key("jp-2"))  # its key, so that no key is asked for
    fake_deps.lightsail.fresh_addresses = [OTHER]
    assert run_up(fake_deps, "jp-2", aws="jp-tokyo") == 0
    fake_deps.terminal.clear()
    fake_deps.lightsail.calls.clear()


# --- the menu --------------------------------------------------------------------------------


def test_the_menu_lists_free_ips_first_then_attached_ones_each_by_name(fake_deps):
    add_ip(fake_deps, "b-free", "203.0.113.51")
    add_ip(fake_deps, "web-ip", "203.0.113.53", "web1")
    add_ip(fake_deps, "a-free", "203.0.113.52")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert menu_lines(fake_deps) == [
        f"1. {NEW}",
        "2. a-free  203.0.113.52  空闲（cnsr rm 时保留）",
        "3. b-free  203.0.113.51  空闲（cnsr rm 时保留）",
        "4. web-ip  203.0.113.53  绑在实例 web1 上（cnsr rm 时保留）",
    ]
    node = fake_deps.node()
    assert (node.static_ip_name, node.static_ip_reused) == ("jp-1-ip", False)
    assert fake_deps.lightsail.ips["jp-1-ip"] == "jp-1"


def test_a_free_ip_of_the_nodes_name_is_offered_as_the_default_and_taken_without_asking_again(fake_deps):
    add_ip(fake_deps, "jp-1-ip", "203.0.113.54")
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert menu_lines(fake_deps) == [
        "1. 接管空闲的静态 IP jp-1-ip（cnsr rm 时释放）",
        "2. spare  203.0.113.50  空闲（cnsr rm 时保留）",
    ]
    assert len(fake_deps.terminal.prompts) == 1  # step 1 does not ask about jp-1-ip again
    node = fake_deps.node()
    assert (node.static_ip_name, node.static_ip_reused, node.ipv4) == ("jp-1-ip", False, "203.0.113.54")
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps) == 0
    assert fake_deps.lightsail.ips == {"spare": None}  # taken over: rm releases it


def test_an_ip_of_the_nodes_name_on_another_instance_cannot_be_the_default(fake_deps):
    add_ip(fake_deps, "jp-1-ip", "203.0.113.54", "web1")
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1", "2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    first, second = menu_lines(fake_deps)
    assert first.startswith("1. ") and first.endswith("（同名静态 IP jp-1-ip 已绑在实例 web1 上，请换一个别名）")
    assert second == "2. spare  203.0.113.50  空闲（cnsr rm 时保留）"
    assert "第 1 项同名静态 IP jp-1-ip 已绑在实例 web1 上，请换一个别名，不能选" in fake_deps.terminal.screen
    assert fake_deps.node().static_ip_name == "spare"


def test_the_only_choice_being_an_ip_of_the_nodes_name_on_another_instance_stops_before_anything_is_made(fake_deps):
    add_ip(fake_deps, "jp-1-ip", "203.0.113.54", "web1")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert not asked(fake_deps) and fake_deps.terminal.prompts == []
    assert "同名静态 IP jp-1-ip 已绑在实例 web1 上，请换一个别名" in fake_deps.terminal.text
    assert fake_deps.lightsail.instances == {} and "ensure_key_pair" not in fake_deps.lightsail.names()
    assert fake_deps.node() is None


def other_node_records_the_ip_of_the_nodes_name(fake_deps) -> None:
    """jp-2's record names jp-1-ip, now free (jp-2's own IP is gone and its instance lost it)."""
    deploy_other(fake_deps)
    other = fake_deps.node("jp-2")
    other.static_ip_name = "jp-1-ip"
    fake_deps.store(other)
    del fake_deps.lightsail.ips["jp-2-ip"], fake_deps.lightsail.addresses["jp-2-ip"]
    add_ip(fake_deps, "jp-1-ip", "203.0.113.54")


def test_a_free_ip_of_the_nodes_name_recorded_by_another_cnsr_node_cannot_be_the_default(fake_deps):
    other_node_records_the_ip_of_the_nodes_name(fake_deps)
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1", "2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    first, second = menu_lines(fake_deps)
    assert first.startswith("1. ") and first.endswith("（同名静态 IP jp-1-ip 是 cnsr 节点 jp-2 在用的地址，请换一个别名）")
    assert second == "2. spare  203.0.113.50  空闲（cnsr rm 时保留）"
    assert "第 1 项同名静态 IP jp-1-ip 是 cnsr 节点 jp-2 在用的地址，请换一个别名，不能选" in fake_deps.terminal.screen
    assert fake_deps.node().static_ip_name == "spare"
    assert fake_deps.lightsail.ips["jp-1-ip"] is None


def test_the_only_choice_being_an_ip_of_the_nodes_name_recorded_by_another_cnsr_node_stops_early(fake_deps):
    other_node_records_the_ip_of_the_nodes_name(fake_deps)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert not asked(fake_deps) and fake_deps.terminal.prompts == []
    assert "同名静态 IP jp-1-ip 是 cnsr 节点 jp-2 在用的地址，请换一个别名" in fake_deps.terminal.text
    assert fake_deps.lightsail.ips["jp-1-ip"] is None and "ensure_key_pair" not in fake_deps.lightsail.names()
    assert fake_deps.node() is None


# --- when the menu is not shown --------------------------------------------------------------


def test_a_region_without_static_ips_asks_nothing_and_reads_no_records(fake_deps, monkeypatch):
    read: list[str] = []
    original = Inventory.aliases
    monkeypatch.setattr(Inventory, "aliases", lambda self: read.append("aliases") or original(self))
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert not asked(fake_deps) and fake_deps.terminal.prompts == []
    assert read == []


def test_a_region_with_only_other_cnsr_nodes_ips_offers_a_takeover_with_a_new_ip_as_default(fake_deps):
    deploy_other(fake_deps)
    fake_deps.terminal.inputs = [""]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert asked(fake_deps) and menu_lines(fake_deps)[0] == f"1. {NEW}"
    assert fake_deps.node().static_ip_name == "jp-1-ip" and fake_deps.lightsail.ips["jp-2-ip"] == "jp-2"


def test_a_free_ip_of_the_nodes_name_alone_is_asked_about_as_before(fake_deps):
    add_ip(fake_deps, "jp-1-ip", "203.0.113.54")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert not asked(fake_deps)
    assert "没有绑定任何实例的静态 IP jp-1-ip" in fake_deps.terminal.screen


def test_a_given_static_ip_asks_nothing(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    add_ip(fake_deps, "other", "203.0.113.51")
    assert run_up(fake_deps, aws="jp-tokyo", static_ip="203.0.113.50") == 0
    assert not asked(fake_deps) and fake_deps.terminal.prompts == []


def test_without_a_terminal_nothing_is_asked_and_the_node_gets_its_own_ip(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert not asked(fake_deps)
    assert fake_deps.node().static_ip_name == "jp-1-ip" and fake_deps.lightsail.ips["spare"] is None


def test_a_vendor_server_is_not_asked_about_static_ips(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["", "", "已保存"]  # SSH user and port, the console password
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert not asked(fake_deps) and fake_deps.lightsail.calls == []


# --- what cannot be picked -------------------------------------------------------------------


def test_the_recorded_ip_of_another_cnsr_node_is_offered_as_a_takeover_even_when_free(fake_deps):
    deploy_other(fake_deps)
    fake_deps.lightsail.ips["jp-2-ip"] = None  # jp-2's instance was deleted by hand
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert menu_lines(fake_deps) == [
        f"1. {NEW}",
        "2. spare  203.0.113.50  空闲（cnsr rm 时保留）",
        f"3. jp-2-ip  {OTHER}  空闲（接替 cnsr 节点 jp-2）",
    ]
    assert fake_deps.lightsail.ips["jp-2-ip"] is None


def test_an_ip_on_another_cnsr_nodes_instance_is_a_takeover_before_its_name_is_recorded(fake_deps):
    deploy_other(fake_deps)
    other = fake_deps.node("jp-2")
    other.static_ip_name = None  # a crash between attaching the IP and saving its name
    fake_deps.store(other)
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert menu_lines(fake_deps)[-1] == f"3. jp-2-ip  {OTHER}  绑在实例 jp-2 上（接替 cnsr 节点 jp-2）"


def test_an_ip_on_an_instance_aws_does_not_name_cannot_be_picked(fake_deps):
    add_ip(fake_deps, "odd-ip", "203.0.113.55", "")
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert menu_lines(fake_deps)[-1] == "3. odd-ip  203.0.113.55  已绑定（绑在一台没有名字的实例上）"


def test_a_given_static_ip_of_another_cnsr_node_stops_before_anything_is_made(fake_deps):
    deploy_other(fake_deps)
    assert run_up(fake_deps, aws="jp-tokyo", static_ip=OTHER) == 1
    assert "cnsr 节点 jp-2" in fake_deps.terminal.text and fake_deps.terminal.prompts == []
    assert fake_deps.lightsail.instances == {"jp-2": True}
    assert fake_deps.lightsail.ips["jp-2-ip"] == "jp-2"
    assert "ensure_key_pair" not in fake_deps.lightsail.names()


# --- what is picked --------------------------------------------------------------------------


def test_a_free_ip_picked_is_reused_and_rm_only_detaches_it(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    node = fake_deps.node()
    assert (node.static_ip_name, node.static_ip_reused, node.static_ip_from) == ("spare", True, None)
    assert fake_deps.lightsail.ips == {"spare": "jp-1"}
    fake_deps.terminal.inputs = ["1"]
    assert run_rm(fake_deps) == 0
    assert fake_deps.lightsail.ips == {"spare": None}
    assert "release_static_ip" not in fake_deps.lightsail.names()


def test_an_attached_ip_picked_still_asks_before_moving_it(fake_deps):
    add_ip(fake_deps, "Debian-1-ip", "203.0.113.50", "Debian-1")
    fake_deps.terminal.inputs = ["2", "2"]  # pick it, then stop at the move question
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert len(fake_deps.terminal.prompts) == 2 and "Debian-1" in fake_deps.terminal.seen[1].split(MENU)[-1]
    assert "静态 IP 保持原样" in fake_deps.terminal.text
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {"Debian-1-ip": "Debian-1"}


# --- reruns ----------------------------------------------------------------------------------


def test_a_rerun_after_the_cleanup_gave_your_ip_back_asks_again(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["2"]
    fake_deps.server.service_fails = True
    fake_deps.lightsail.delete_instance_errors = ["实例删除失败"]  # so the record stays
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    node = fake_deps.node()
    assert (node.line, node.static_ip_reused, node.static_ip_name) == (None, True, None)
    fake_deps.server.service_fails = False
    fake_deps.lightsail.delete_instance_errors = []
    fake_deps.terminal.clear()
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert asked(fake_deps)
    assert fake_deps.node().static_ip_name == "spare" and "jp-1-ip" not in fake_deps.lightsail.ips


def test_a_normal_rerun_asks_nothing(fake_deps):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.terminal.clear()
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert not asked(fake_deps) and fake_deps.terminal.prompts == []


@pytest.mark.parametrize("picked", ["1", "2"])
def test_the_equivalent_command_never_names_a_static_ip(fake_deps, picked):
    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = [picked]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert "cnsr up jp-1 --aws jp-tokyo --size 2c0.5g --to xray" in fake_deps.terminal.screen
    assert "--static-ip" not in fake_deps.terminal.screen



def test_back_from_the_static_ip_asks_the_size_again(fake_deps):
    from deploy_fakes import SIZE_MENU

    add_ip(fake_deps, "spare", "203.0.113.50")
    fake_deps.terminal.inputs = ["2", "0", "1", "1"]  # size: 2c1g; static IP: back; size: 2c0.5g; static IP: new
    assert run_up(fake_deps, aws="jp-tokyo", size=SIZE_MENU) == 0
    assert fake_deps.terminal.screen.count("AWS 规格（月价含 IPv4 地址）\n") == 2
    assert fake_deps.node().size == "2c0.5g"
