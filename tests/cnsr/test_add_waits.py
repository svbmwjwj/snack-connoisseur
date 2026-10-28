"""Every wait in ``cnsr up`` keeps to its stated limit, counting the time its attempts take."""

from __future__ import annotations

from deploy_fakes import run_up

from cnsr.cloudflare.control import ProbeResult
from cnsr.deploy import steps_host, steps_node, steps_rotate
from cnsr.scan.ipv6_reach import Ipv6Reach
from cnsr.ssh.remote import SshError

ATTEMPT = 10  # what one failing attempt costs (ssh's own ConnectTimeout, say)


def costly(fake_deps, function):
    """``function``, made to move the fake clock on by ATTEMPT seconds per call."""

    def call(*args, **kwargs):
        fake_deps.now += ATTEMPT
        return function(*args, **kwargs)

    return call


def test_the_boot_wait_ends_when_its_three_minutes_are_up(fake_deps):
    fake_deps.server.fail_on["true"] = SshError("port")
    fake_deps.server.failed_attempt_seconds = ATTEMPT
    assert run_up(fake_deps, aws="jp") == 1
    assert "等待 SSH 就绪（最多 3 分钟）" in fake_deps.terminal.text
    # it was 6 minutes when only the pauses between attempts were counted
    assert fake_deps.now <= steps_host.BOOT_WAIT_SECONDS + ATTEMPT


def test_the_outside_probe_ends_when_its_limit_is_up(fake_deps):
    fake_deps.probe_results["198.51.100.20"] = [ProbeResult(ok=False, ms=None, error="连接超时")] * 100
    fake_deps.deps.probe = costly(fake_deps, fake_deps.deps.probe)
    started = []
    original = steps_node._probe

    def watched(ctx, host, wait, note):
        started.append(fake_deps.now)
        return original(ctx, host, wait, note)

    steps_node._probe = watched
    try:
        assert run_up(fake_deps, aws="jp") == 1
    finally:
        steps_node._probe = original
    assert fake_deps.now - started[0] <= steps_node.V4_PROBE_WAIT + ATTEMPT


def test_the_ipv6_probe_ends_when_its_limit_is_up(fake_deps):
    fake_deps.v6_results = [Ipv6Reach(ok=False, ms=None, error="连接超时")] * 100
    calls = []

    def probe(host):
        calls.append(fake_deps.now)
        fake_deps.now += ATTEMPT
        return fake_deps.v6_results.pop(0)

    fake_deps.deps.probe_ipv6 = probe
    run_up(fake_deps, aws="jp")
    assert calls[-1] - calls[0] <= steps_node.V6_PROBE_WAIT


def test_the_public_dns_wait_ends_when_its_limit_is_up(fake_deps):
    fake_deps.public_dns = False
    calls = []
    original = fake_deps.deps.public_resolves

    def resolves(*args):
        calls.append(fake_deps.now)
        fake_deps.now += ATTEMPT
        return original(*args)

    fake_deps.deps.public_resolves = resolves
    assert run_up(fake_deps, aws="jp") == 1
    assert f"等了 {steps_node.PUBLIC_DNS_WAIT} 秒" in fake_deps.terminal.text
    assert calls[-1] - calls[0] <= steps_node.PUBLIC_DNS_WAIT


def test_the_alias_switch_gives_up_when_its_two_minutes_are_up(fake_deps):
    fake_deps.local_dns = False
    calls = []
    original = fake_deps.deps.local_resolves

    def resolves(*args):
        calls.append(fake_deps.now)
        fake_deps.now += ATTEMPT
        return original(*args)

    fake_deps.deps.local_resolves = resolves
    assert run_up(fake_deps, aws="jp") == 0
    assert 2 < len(calls) and calls[-1] - calls[0] <= 120


def test_the_control_check_ends_when_its_five_minutes_are_up(fake_deps, monkeypatch):
    fake_deps.server.control_answers = ["000"]
    original = fake_deps.server.command

    def command(cmd):
        if cmd == ["/opt/cnsr/bin/cnsr-rotate.sh", "check"]:
            fake_deps.now += ATTEMPT
        return original(cmd)

    monkeypatch.setattr(fake_deps.server, "command", command)
    assert run_up(fake_deps, aws="jp") == 1
    checks = fake_deps.server.control_checks
    assert checks * (ATTEMPT + steps_rotate.CHECK_INTERVAL_SECONDS) <= steps_rotate.CHECK_SECONDS + 2 * ATTEMPT
    assert "等了 5 分钟" in fake_deps.terminal.text
