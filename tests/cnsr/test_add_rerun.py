"""``cnsr add`` on a node that already runs: what a rerun refills, skips or repeats."""

from __future__ import annotations

import pytest
from deploy_fakes import AWS_IP, run_add

from cnsr.scan.egress import Egress
from cnsr.server.xray import BACKUPS_PATH
from cnsr.ssh.remote import SshError

PROXIED = Egress("203.0.113.5", "198.51.100.9")
UNKNOWN = Egress(None, "203.0.113.5")


def deploy_with_few_backups(fake_deps) -> str:
    """Deploy jp-1 on AWS with only three backups, so that a rerun wants to scan again."""
    fake_deps.server.latency = {f"site{i}.example.com": 50 - i for i in range(4)}
    assert run_add(fake_deps, aws="jp") == 0
    return fake_deps.server.files[BACKUPS_PATH]


# --- refilling the backups needs a direct connection, but never blocks the rerun ----------


@pytest.mark.parametrize("seen", [PROXIED, UNKNOWN])
def test_a_refill_behind_a_proxy_is_skipped_with_a_warning(fake_deps, seen):
    backups = deploy_with_few_backups(fake_deps)
    fake_deps.egress_result = seen
    prompts = len(fake_deps.terminal.prompts)

    assert run_add(fake_deps, aws="jp") == 0

    text = fake_deps.terminal.text
    assert "警告：备选未补充" in text and "关闭代理后重跑" in text
    assert len(fake_deps.calls["scan"]) == 1 and len(fake_deps.terminal.prompts) == prompts
    assert fake_deps.server.files[BACKUPS_PATH] == backups
    assert fake_deps.node().line


def test_a_first_deployment_behind_a_proxy_still_stops(fake_deps):
    fake_deps.egress_result = PROXIED
    fake_deps.terminal.inputs = ["2"]
    assert run_add(fake_deps, aws="jp") == 1
    assert "关闭代理" in fake_deps.terminal.text and "备选未补充" not in fake_deps.terminal.text


# --- an instance created in this run gets time to start sshd -----------------------------


def sshd_starts_after_a_while(fake_deps, error: SshError) -> None:
    """Logins fail with ``error`` until cnsr has waited once."""
    fake_deps.server.fail_on["true"] = error

    def sleep(seconds):
        fake_deps.sleeps.append(seconds)
        fake_deps.server.fail_on.pop("true", None)

    fake_deps.deps.sleep = sleep


def test_an_instance_recreated_on_a_rerun_gets_the_boot_wait_and_a_fresh_host_key(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    domain = fake_deps.node().domain
    fake_deps.lightsail.instances.clear()  # deleted by hand; the record still names it
    sshd_starts_after_a_while(fake_deps, SshError("port"))
    forgotten = len(fake_deps.calls["forget"])

    assert run_add(fake_deps, aws="jp") == 0

    assert fake_deps.lightsail.instances == {"jp-1": True}
    assert "等待 SSH 就绪" in fake_deps.terminal.text and 10 in fake_deps.sleeps
    assert fake_deps.calls["forget"][forgotten:] == [(AWS_IP, 22), (domain, 22)]


def test_an_existing_instance_on_a_rerun_gets_no_boot_wait(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    fake_deps.server.fail_on["true"] = SshError("port")
    assert run_add(fake_deps, aws="jp") == 1
    assert "等待 SSH 就绪" not in fake_deps.terminal.text


def test_connection_closed_while_a_new_instance_boots_is_retried(fake_deps):
    sshd_starts_after_a_while(fake_deps, SshError("other", "SSH 连接失败（退出码 255）：\nkex_exchange_identification: Connection closed by remote host"))
    assert run_add(fake_deps, aws="jp") == 0
    assert fake_deps.sleeps[0] == 10
