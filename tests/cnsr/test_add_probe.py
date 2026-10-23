"""``cnsr add``: testing the node's 443 from outside, and what that means for its AAAA record.

The control plane tests IPv4; IPv6 is tested from this computer, since the Worker cannot reach
IPv6 addresses. Only a test that reached IPv6 and found the node silent removes the AAAA record.
When the tester cannot be reached, or this computer has no IPv6, nothing is blamed on the node
and nothing is deleted. A node whose IPv6 failed keeps no AAAA on reruns until a test passes.
"""

from __future__ import annotations

from deploy_fakes import AWS_IP, AWS_IPV6, VPS_IP, run_add

from cnsr.cloudflare.control import ProbeResult
from cnsr.scan.ipv6_reach import Ipv6Reach

DOWN = ProbeResult(ok=False, ms=None, error="连接超时")
NO_CONTROL = ProbeResult(ok=False, ms=None, error="无法连接控制端", control_reached=False)
V6_DOWN = Ipv6Reach(ok=False, ms=None, error="连接超时")
NO_V6_ROUTE = Ipv6Reach(ok=False, ms=None, error="这台电脑连不上任何 IPv6 地址", tested=False)


def dns_writes(fake_deps) -> list[str]:
    return [request.method for request in fake_deps.dns.requests if request.method != "GET"]


def test_an_unreachable_control_plane_is_not_blamed_on_the_node(fake_deps):
    # A vendor server, since a first AWS deployment that fails deletes its records anyway.
    fake_deps.probe_results[VPS_IP] = [NO_CONTROL] * 100
    fake_deps.terminal.inputs = ["", "", "已保存"]
    assert run_add(fake_deps, ip=VPS_IP) == 1
    text = fake_deps.terminal.text
    assert "连不上控制端" in text and "不是节点" in text and "防火墙是否放行 443" not in text
    assert len(fake_deps.dns.of_type("AAAA")) == 1  # nothing was deleted
    assert fake_deps.node().line is None


def test_ipv6_is_tested_from_this_computer_not_by_the_control_plane(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    assert AWS_IPV6 not in fake_deps.calls["probe"]
    assert fake_deps.calls["probe_ipv6"] == [AWS_IPV6]
    assert len(fake_deps.dns.of_type("AAAA")) == 1


def test_without_ipv6_on_this_computer_the_aaaa_record_is_kept(fake_deps):
    fake_deps.v6_results = [NO_V6_ROUTE]
    assert run_add(fake_deps, aws="jp") == 0
    assert fake_deps.calls["probe_ipv6"] == [AWS_IPV6]  # no route does not come back by waiting
    assert len(fake_deps.dns.of_type("AAAA")) == 1
    node = fake_deps.node()
    assert node.ipv6 == AWS_IPV6 and "AAAA" in node.dns_record_ids and node.ipv6_probe_failed is False
    text = fake_deps.terminal.text
    assert "警告：" in text and "没有可用的 IPv6" in text and "AAAA 记录保持不变" in text
    assert "已删除 AAAA" not in text


def test_behind_a_tun_proxy_the_ipv6_result_is_not_trusted(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    fake_deps.deps.fake_ip_dns = lambda: True  # a TUN proxy is on now
    fake_deps.v6_results = [V6_DOWN] * 100
    fake_deps.calls["probe_ipv6"].clear()

    assert run_add(fake_deps, aws="jp") == 0

    assert fake_deps.calls["probe_ipv6"] == []
    assert len(fake_deps.dns.of_type("AAAA")) == 1 and fake_deps.node().ipv6_probe_failed is False
    assert "AAAA 记录保持不变" in fake_deps.terminal.text


def test_a_silent_ipv6_is_retried_then_its_aaaa_record_is_deleted(fake_deps):
    fake_deps.v6_results = [V6_DOWN] * 100
    assert run_add(fake_deps, aws="jp") == 0
    assert len(fake_deps.calls["probe_ipv6"]) == 4  # once, then every 10 seconds for 30 seconds
    assert fake_deps.dns.of_type("AAAA") == []
    node = fake_deps.node()
    assert node.ipv6 is None and "AAAA" not in node.dns_record_ids and node.ipv6_probe_failed is True
    assert "已删除 AAAA" in fake_deps.terminal.text


def test_a_failed_ipv6_is_not_published_again_by_a_rerun_while_it_fails(fake_deps):
    fake_deps.v6_results = [V6_DOWN] * 100
    assert run_add(fake_deps, aws="jp") == 0
    assert fake_deps.dns.of_type("AAAA") == [] and fake_deps.node().ipv6_probe_failed is True
    fake_deps.dns.requests.clear()
    fake_deps.terminal.clear()

    assert run_add(fake_deps, aws="jp") == 0

    assert dns_writes(fake_deps) == []  # no AAAA created in step_connect, none deleted after the probe
    assert fake_deps.dns.of_type("AAAA") == [] and fake_deps.node().ipv6_probe_failed is True
    assert "已删除 AAAA" not in fake_deps.terminal.text  # there was nothing to delete


def test_a_failed_ipv6_stays_off_when_this_computer_cannot_test_it(fake_deps):
    fake_deps.v6_results = [V6_DOWN] * 100
    assert run_add(fake_deps, aws="jp") == 0
    fake_deps.v6_results = [NO_V6_ROUTE]
    fake_deps.dns.requests.clear()
    fake_deps.terminal.clear()

    assert run_add(fake_deps, aws="jp") == 0

    assert dns_writes(fake_deps) == []
    node = fake_deps.node()
    assert node.ipv6 is None and node.ipv6_probe_failed is True
    text = fake_deps.terminal.text
    assert "没有可用的 IPv6" in text and "只走 IPv4" in text and "已删除 AAAA" not in text


def test_ipv6_is_published_again_once_its_probe_passes(fake_deps):
    fake_deps.v6_results = [V6_DOWN] * 100
    assert run_add(fake_deps, aws="jp") == 0
    fake_deps.v6_results = []  # IPv6 works now
    fake_deps.calls["public"].clear()

    assert run_add(fake_deps, aws="jp") == 0

    (aaaa,) = fake_deps.dns.of_type("AAAA")
    node = fake_deps.node()
    assert aaaa["content"] == AWS_IPV6 and node.dns_record_ids["AAAA"] == aaaa["id"]
    assert node.ipv6 == AWS_IPV6 and node.ipv6_probe_failed is False
    assert [rtype for _, rtype, _ in fake_deps.calls["public"]] == ["A", "AAAA"]
