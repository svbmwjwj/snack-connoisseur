"""A Hetzner Primary IP this computer cannot reach at all (blocked) is swapped for a new one
while the server is off; a swap that fails part way leaves cleanup and ``cnsr rm`` all they need."""

from __future__ import annotations

import pytest
from deploy_fakes import run_rm, run_up
from hetzner_fakes import HZ_IP

from cnsr import features
from cnsr.deploy import reach
from cnsr.errors import CnsrError
from cnsr.ssh.config import read_block

FRESH = ["198.51.100.71", "198.51.100.72", "198.51.100.73"]
CLEANED = "已清理干净，不会继续计费"
SWAP_ORDER = [
    ("power_off", "jp-1"),
    ("unassign_primary_ip", "jp-1-ip"),
    ("create_assigned_primary_ip", "jp-1-ip-new", "jp-1", "jp-1"),
    ("delete_primary_ip", "jp-1-ip", "jp-1"),
    ("rename_primary_ip", "jp-1-ip-new", "jp-1-ip"),
    ("power_on", "jp-1"),
]
SWAP_CALLS = {call[0] for call in SWAP_ORDER}


@pytest.fixture
def hz(fake_deps):
    fake_deps.hcloud_env = "hz-env-token-0001"
    fake_deps.terminal.inputs = ["已保存"]
    return fake_deps


def deploy(fakes) -> int:
    return run_up(fakes, hetzner="sg", size="1c1g", features=list(features.ALL))


def nothing_left(fakes) -> bool:
    hetzner = fakes.hetzner
    return (
        hetzner.servers == {} and hetzner.ips == {} and hetzner.firewalls == {} and fakes.dns.records == {}
        and fakes.node() is None and read_block(fakes.deps.ssh_config, "jp-1") is None
    )  # fmt: skip


def fail_once(fakes, method: str, outcome) -> None:
    """Make the fake's ``method`` fail on its first call only: raise ``outcome``, or return it."""
    real = getattr(fakes.hetzner, method)
    state = {"failed": False}

    def once(*args):
        if state["failed"]:
            return real(*args)
        state["failed"] = True
        fakes.hetzner.calls.append((method, *args))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    setattr(fakes.hetzner, method, once)


# --- the swap ------------------------------------------------------------------------------


def test_a_blocked_primary_ip_is_swapped_while_the_server_is_off(hz):
    hz.server.blocked_hosts = {HZ_IP}
    hz.hetzner.fresh_addresses = [HZ_IP, FRESH[0]]
    assert deploy(hz) == 0
    calls = hz.hetzner.calls
    positions = [calls.index(call) for call in SWAP_ORDER]
    assert positions == sorted(positions)
    node = hz.node()
    assert (node.ipv4, node.static_ip_name) == (FRESH[0], "jp-1-ip") and node.line
    assert hz.hetzner.ips == {"jp-1-ip": True} and hz.hetzner.assigned == {"jp-1-ip": "jp-1"}
    assert hz.hetzner.off == set()
    assert (FRESH[0], 22) in hz.calls["forget"]
    text = hz.terminal.text
    assert f"Primary IP {HZ_IP} 从这台电脑整个连不上" in text and "关机后换一个新 IP 再开机（第 1 次）" in text
    assert f"新的 Primary IP：{FRESH[0]}" in text


def test_the_new_address_differs_without_asking_twice(hz):
    hz.server.blocked_hosts = {HZ_IP}
    assert deploy(hz) == 0
    assert hz.hetzner.names().count("create_assigned_primary_ip") == 1
    assert hz.node().ipv4 != HZ_IP


def test_the_new_address_gets_the_boot_wait_not_a_password_login(hz):
    hz.server.blocked_hosts = {HZ_IP}
    hz.hetzner.fresh_addresses = [HZ_IP, FRESH[0]]
    power_on = hz.hetzner.power_on

    def booting(name):  # sshd comes up again before it takes the key
        power_on(name)
        hz.server.key_installed = False

    def key_arrives(seconds):
        hz.sleeps.append(seconds)
        hz.server.key_installed = True

    hz.hetzner.power_on, hz.deps.sleep = booting, key_arrives
    assert deploy(hz) == 0
    text = hz.terminal.text
    assert text.index("新的 Primary IP") < text.index("等待 SSH 就绪")
    assert hz.calls["install_key"] == [] and "root 密码" not in text


def test_the_swaps_stop_after_three_and_cleanup_deletes_everything(hz):
    hz.server.blocked_hosts = {HZ_IP, *FRESH}
    hz.hetzner.fresh_addresses = [HZ_IP, *FRESH]
    assert deploy(hz) == 1
    assert hz.hetzner.names().count("create_assigned_primary_ip") == reach.IP_SWAPS
    text = hz.terminal.text
    assert f"已经换了 {reach.IP_SWAPS} 次 IP" in text and "联系服务器厂商" not in text
    assert nothing_left(hz) and CLEANED in text


def test_a_deployed_node_keeps_its_primary_ip(hz):
    assert deploy(hz) == 0
    node = hz.node()
    hz.server.blocked_hosts = {node.ipv4, node.domain}
    hz.hetzner.calls.clear()
    assert run_up(hz, hetzner="sg") == 1
    assert not SWAP_CALLS & set(hz.hetzner.names())
    text = hz.terminal.text
    assert "整个连不上" in text and "请运行 cnsr rm jp-1 后重新部署" in text
    assert hz.hetzner.servers == {"jp-1": True}


def test_a_node_hardened_alone_stays_on_and_keeps_its_primary_ip(hz):
    # No client line, but in use: the cloud firewall drops 80 and 443, so a blocked SSH port looks blocked.
    assert run_up(hz, hetzner="sg", size="1c1g", features=[features.HARDEN]) == 0
    node = hz.node()
    assert node.line is None
    hz.server.blocked_hosts = {node.ipv4, node.domain}
    hz.hetzner.calls.clear()
    assert run_up(hz, hetzner="sg", features=[features.HARDEN]) == 1
    assert not SWAP_CALLS & set(hz.hetzner.names())
    assert hz.hetzner.servers == {"jp-1": True} and hz.hetzner.off == set()
    assert hz.node().ipv4 == node.ipv4


def test_can_swap_takes_a_hetzner_node_that_was_never_deployed(hz, monkeypatch):
    hz.server.blocked_hosts = {HZ_IP}
    seen = []
    real = reach.can_swap

    def spy(ctx):
        seen.append((ctx.req.source, ctx.node.static_ip_name, ctx.node.line, ctx.ip_swaps, real(ctx)))
        return seen[-1][-1]

    monkeypatch.setattr(reach, "can_swap", spy)
    assert deploy(hz) == 0
    assert seen[0] == ("hetzner", "jp-1-ip", None, 0, True)


# --- a swap that fails part way ------------------------------------------------------------

FAILURES = [
    ("power_off", CnsrError("关机失败")),
    ("unassign_primary_ip", CnsrError("解绑失败")),
    ("create_assigned_primary_ip", CnsrError("新建 Primary IP 失败")),
    ("delete_primary_ip", ["Hetzner API 请求失败（HTTP 423）：资源被锁定"]),
    ("rename_primary_ip", CnsrError("改名失败")),
    ("power_on", CnsrError("开机失败")),
]


@pytest.mark.parametrize(("method", "outcome"), FAILURES, ids=[method for method, _ in FAILURES])
def test_a_swap_failing_part_way_is_cleaned_up(hz, method, outcome):
    hz.server.blocked_hosts = {HZ_IP}
    fail_once(hz, method, outcome)
    assert deploy(hz) == 1
    assert nothing_left(hz) and CLEANED in hz.terminal.text


# The record names the IP that still needs deleting on its own: the blocked one until it is
# gone (the new one is assigned to the server and goes with it), then the new one.
RECORDED = {
    "power_off": ("jp-1-ip", HZ_IP),
    "unassign_primary_ip": ("jp-1-ip", HZ_IP),
    "create_assigned_primary_ip": ("jp-1-ip", HZ_IP),
    "delete_primary_ip": ("jp-1-ip", HZ_IP),
    "rename_primary_ip": ("jp-1-ip-new", FRESH[0]),
    "power_on": ("jp-1-ip", FRESH[0]),
}


@pytest.mark.parametrize(("method", "outcome"), FAILURES, ids=[method for method, _ in FAILURES])
def test_rm_finishes_what_a_failed_swap_and_cleanup_left(hz, method, outcome):
    hz.server.blocked_hosts = {HZ_IP}
    hz.hetzner.fresh_addresses = [HZ_IP, FRESH[0]]
    fail_once(hz, method, outcome)
    hz.hetzner.delete_instance_errors = ["Hetzner API 请求失败（HTTP 423）：资源被锁定"]
    assert deploy(hz) == 1
    node = hz.node()
    assert (node.static_ip_name, node.ipv4) == RECORDED[method]
    hz.hetzner.delete_instance_errors = []
    hz.terminal.inputs = ["1"]
    assert run_rm(hz) == 0
    assert nothing_left(hz)
