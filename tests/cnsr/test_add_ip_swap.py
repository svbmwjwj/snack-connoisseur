"""An AWS static IP this computer cannot reach at all (blocked) is swapped for a new one."""

from __future__ import annotations

from deploy_fakes import VPS_IP, run_up
from lightsail_fakes import AWS_IP

from cnsr.deploy import reach

VPS_ANSWERS = ["", "", "已保存"]  # SSH user and port as offered, then the new password saved
FRESH = ["203.0.113.71", "203.0.113.72", "203.0.113.73"]


def released(fake_deps) -> list:
    return [call for call in fake_deps.lightsail.calls if call[0] == "release_static_ip"]


def test_a_blocked_ip_is_swapped_for_a_new_one(fake_deps):
    fake_deps.server.blocked_hosts = {AWS_IP}
    fake_deps.lightsail.fresh_addresses = [AWS_IP, FRESH[0]]
    assert run_up(fake_deps, aws="jp") == 0
    assert released(fake_deps) == [("release_static_ip", "jp-1-ip")]
    assert fake_deps.node().ipv4 == FRESH[0]
    # 80: a new Lightsail instance's firewall opens it (and 22) only; nothing listens there
    tried = fake_deps.calls["local_tcp"]
    assert tried.count((AWS_IP, 80)) == 2 and tried.count((AWS_IP, 443)) == 2  # every try timed out
    text = fake_deps.terminal.text
    assert f"{AWS_IP} 从这台电脑整个连不上" in text and "换一个新 IP（第 1 次）" in text


def test_the_swaps_stop_after_three(fake_deps):
    fake_deps.server.blocked_hosts = {AWS_IP, *FRESH}
    fake_deps.lightsail.fresh_addresses = [AWS_IP, *FRESH]
    assert run_up(fake_deps, aws="jp") == 1
    assert len(released(fake_deps)) >= reach.IP_SWAPS  # the swaps, then the clean-up
    assert f"已经换了 {reach.IP_SWAPS} 次 IP" in fake_deps.terminal.text


def test_your_own_static_ip_is_never_swapped(fake_deps):
    kept = "203.0.113.50"
    fake_deps.lightsail.ips["spare"] = None
    fake_deps.lightsail.addresses["spare"] = kept
    fake_deps.server.blocked_hosts = {kept}
    assert run_up(fake_deps, aws="jp", static_ip=kept) == 1
    assert released(fake_deps) == [] and fake_deps.lightsail.ips == {"spare": None}
    text = fake_deps.terminal.text
    assert f"{kept} 从这台电脑整个连不上" in text and "不带 --static-ip" in text


def test_a_vendor_servers_blocked_ip_is_for_the_vendor(fake_deps):
    fake_deps.server.blocked_hosts = {VPS_IP}
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert "联系服务器厂商更换 IP" in fake_deps.terminal.text
    assert {(VPS_IP, 80), (VPS_IP, 443)} <= set(fake_deps.calls["local_tcp"])


def test_a_deployed_node_keeps_its_ip(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    fake_deps.server.blocked_hosts = {fake_deps.node().ipv4, fake_deps.node().domain}
    assert run_up(fake_deps, aws="jp") == 1
    assert released(fake_deps) == []
    assert "整个连不上" in fake_deps.terminal.text


def test_the_same_address_again_counts_as_a_swap(fake_deps):
    fake_deps.server.blocked_hosts = {AWS_IP}
    fake_deps.lightsail.fresh_addresses = [AWS_IP, AWS_IP, FRESH[0]]
    assert run_up(fake_deps, aws="jp") == 0
    assert len(released(fake_deps)) == 2 and fake_deps.node().ipv4 == FRESH[0]


def test_the_blocked_ip_is_detached_and_settled_before_it_is_released(fake_deps):
    fake_deps.server.blocked_hosts = {AWS_IP}
    fake_deps.lightsail.fresh_addresses = [AWS_IP, FRESH[0]]
    assert run_up(fake_deps, aws="jp") == 0
    names = fake_deps.lightsail.names()
    assert names.index("detach_static_ip") < names.index("release_static_ip")
