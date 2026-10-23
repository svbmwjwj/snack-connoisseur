"""``cnsr add --aws <region> --static-ip <IPv4>``: a static IP the user already has becomes the node's."""

from __future__ import annotations

import pytest
from deploy_fakes import run_add, run_rm

from cnsr.errors import CnsrError

KEPT = "203.0.113.50"


@pytest.fixture
def spare(fake_deps):
    """A free static IP of the user's in the region."""
    fake_deps.lightsail.ips["spare"] = None
    fake_deps.lightsail.addresses["spare"] = KEPT
    return fake_deps


@pytest.fixture
def on_debian(fake_deps):
    """The user's static IP, attached to the instance it serves today."""
    fake_deps.lightsail.ips["Debian-1-ip"] = "Debian-1"
    fake_deps.lightsail.addresses["Debian-1-ip"] = KEPT
    return fake_deps


def calls(fake_deps, name: str) -> list[tuple]:
    return [call for call in fake_deps.lightsail.calls if call[0] == name]


def test_a_free_static_ip_is_used_as_it_is(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    node = spare.node()
    assert spare.terminal.prompts == []
    assert spare.lightsail.ips == {"spare": "jp-1"}  # no <alias>-ip of its own
    assert (node.static_ip_name, node.static_ip_reused, node.static_ip_from, node.ipv4) == ("spare", True, None, KEPT)


def test_a_static_ip_on_another_instance_moves_only_after_you_agree(on_debian):
    on_debian.terminal.inputs = ["1"]
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 0
    (question,) = on_debian.terminal.seen
    assert "Debian-1" in question and KEPT in question
    names = on_debian.lightsail.names()
    # moved once the new instance runs, so the old one serves until then
    assert names.index("wait_running") < names.index("detach_static_ip") < names.index("ensure_static_ip")
    assert calls(on_debian, "detach_static_ip") == [("detach_static_ip", "Debian-1-ip")]
    assert on_debian.lightsail.ips == {"Debian-1-ip": "jp-1"}
    node = on_debian.node()
    assert (node.static_ip_name, node.static_ip_from, node.ipv4) == ("Debian-1-ip", "Debian-1", KEPT)


def test_declining_the_move_stops_before_any_instance_is_created(on_debian):
    on_debian.terminal.inputs = ["2"]
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 1
    assert on_debian.lightsail.instances == {} and on_debian.lightsail.ips == {"Debian-1-ip": "Debian-1"}
    assert "静态 IP 保持原样" in on_debian.terminal.text


def test_without_a_terminal_the_move_is_refused(on_debian):
    on_debian.deps.ui = on_debian.terminal.ui(interactive=False)
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 1
    assert "没有确认" in on_debian.terminal.text
    assert on_debian.lightsail.instances == {} and on_debian.lightsail.ips == {"Debian-1-ip": "Debian-1"}


def test_an_address_the_region_has_no_static_ip_for_stops_the_add(fake_deps):
    assert run_add(fake_deps, aws="jp", static_ip=KEPT) == 1
    assert f"AWS 东京区域里没有地址为 {KEPT} 的静态 IP" in fake_deps.terminal.text
    assert fake_deps.lightsail.instances == {}


def test_a_static_ip_that_moved_elsewhere_meanwhile_is_left_alone(on_debian, monkeypatch):
    on_debian.terminal.inputs = ["1"]
    original = on_debian.lightsail.wait_running

    def someone_moves_it(name):
        original(name)
        on_debian.lightsail.ips["Debian-1-ip"] = "Other-1"

    monkeypatch.setattr(on_debian.lightsail, "wait_running", someone_moves_it)
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 1
    assert calls(on_debian, "detach_static_ip") == []
    assert "现在绑在实例 Other-1 上" in on_debian.terminal.text


def test_a_failed_first_deployment_gives_the_static_ip_back(on_debian):
    on_debian.terminal.inputs = ["1"]
    on_debian.server.service_fails = True  # step 8 fails, after the move
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 1
    assert on_debian.lightsail.instances == {}  # the new instance is deleted ...
    assert on_debian.lightsail.ips == {"Debian-1-ip": "Debian-1"}  # ... and the IP is back, never released
    assert calls(on_debian, "release_static_ip") == []
    assert "已把静态 IP 绑回实例 Debian-1" in on_debian.terminal.text
    assert "按小时收费" not in on_debian.terminal.screen  # the IP is attached again at once


def test_a_deployment_that_fails_before_the_move_does_not_claim_to_give_the_ip_back(on_debian, monkeypatch):
    on_debian.terminal.inputs = ["1"]

    def not_running(name):
        raise CnsrError("实例没有进入运行状态")

    monkeypatch.setattr(on_debian.lightsail, "wait_running", not_running)  # before the IP moves
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 1
    assert on_debian.lightsail.ips == {"Debian-1-ip": "Debian-1"}
    assert "已把静态 IP 绑回" not in on_debian.terminal.text and "保持不动" in on_debian.terminal.text


def test_rm_keeps_a_reused_static_ip(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    spare.terminal.inputs = ["jp-1"]
    assert run_rm(spare) == 0
    assert "  — 静态 IP spare（你原有的，只解绑、不释放）" in spare.terminal.text
    assert "删除 AWS 实例（静态 IP 保留）" in spare.terminal.text
    assert spare.lightsail.instances == {} and spare.lightsail.ips == {"spare": None}
    assert calls(spare, "release_static_ip") == []
    text = spare.terminal.text
    assert f"静态 IP spare（{KEPT}）是你原有的，已解绑并保留" in text and "按小时收费" in text


def test_a_rerun_may_repeat_the_static_ip(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    assert run_add(spare, aws="jp") == 0
    assert spare.lightsail.ips == {"spare": "jp-1"}


def test_a_rerun_with_another_static_ip_is_refused(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    assert run_add(spare, aws="jp", static_ip="203.0.113.51") == 1
    assert "和这次的参数不一致" in spare.terminal.text


def test_a_node_deployed_with_its_own_static_ip_refuses_another(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    assert run_add(fake_deps, aws="jp", static_ip=KEPT) == 1
    assert "和这次的参数不一致" in fake_deps.terminal.text


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"ip": "198.51.100.7", "static_ip": KEPT}, "--static-ip 只能和 --aws 一起用"),
        ({"static_ip": KEPT}, "--static-ip 只能和 --aws 一起用"),
        ({"aws": "jp", "static_ip": "203.0.113"}, "--static-ip"),
    ],
)
def test_static_ip_arguments_are_checked_before_anything_happens(fake_deps, args, message):
    assert run_add(fake_deps, **args) == 1
    assert message in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == []


def test_a_later_run_asks_again_before_moving_the_ip_off_another_instance(on_debian):
    on_debian.terminal.inputs = ["1"]
    assert run_add(on_debian, aws="jp", static_ip=KEPT) == 0
    on_debian.lightsail.ips["Debian-1-ip"] = "Debian-1"  # the user put it back by hand
    on_debian.terminal.inputs = ["2"]
    assert run_add(on_debian, aws="jp") == 1
    assert len(on_debian.terminal.seen) == 2 and "Debian-1" in on_debian.terminal.seen[1]
    assert on_debian.lightsail.ips == {"Debian-1-ip": "Debian-1"}


def test_a_rerun_after_a_partial_rm_never_marks_cnsrs_own_ip_as_reused(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    spare.lightsail.delete_instance_errors = ["实例删除失败"]
    spare.terminal.inputs = ["jp-1"]
    assert run_rm(spare) == 1  # the IP is detached and kept, the instance and the record stay
    spare.lightsail.delete_instance_errors = []
    assert run_add(spare, aws="jp") == 0
    node = spare.node()
    assert (node.static_ip_name, node.static_ip_reused, node.static_ip_from) == ("jp-1-ip", False, None)
    assert spare.lightsail.ips == {"spare": None, "jp-1-ip": "jp-1"}


def test_a_rerun_after_a_partial_rm_may_take_the_kept_ip_again(spare):
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    spare.lightsail.delete_instance_errors = ["实例删除失败"]
    spare.terminal.inputs = ["jp-1"]
    assert run_rm(spare) == 1
    spare.lightsail.delete_instance_errors = []
    assert run_add(spare, aws="jp", static_ip=KEPT) == 0
    assert spare.node().static_ip_name == "spare" and spare.lightsail.ips == {"spare": "jp-1"}


def test_an_interrupted_clean_up_says_where_the_ip_belongs(on_debian, monkeypatch):
    on_debian.terminal.inputs = ["1"]
    on_debian.server.service_fails = True

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("cnsr.commands.add.remove_node", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_add(on_debian, aws="jp", static_ip=KEPT)
    assert "静态 IP Debian-1-ip 原来绑在实例 Debian-1 上" in on_debian.terminal.text
