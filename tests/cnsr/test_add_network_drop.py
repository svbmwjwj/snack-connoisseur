"""A connection that drops mid-step: once the server answers again, the step runs again."""

from __future__ import annotations

import functools

from deploy_fakes import run_up

from cnsr.cloudflare.api import CloudflareError
from cnsr.deploy import steps
from cnsr.ssh.remote import SshError

IDENTITY = "put /opt/cnsr/identity.env"


def dropped() -> SshError:
    return SshError("port", "Read from remote host: Operation timed out\nclient_loop: send disconnect: Broken pipe")


def test_a_step_cut_off_by_a_dropped_connection_runs_again(fake_deps):
    fake_deps.server.fail_once[IDENTITY] = dropped()
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert "与服务器的连接中断了，网络恢复后重跑这一步（部署 Xray）" in fake_deps.terminal.text
    assert fake_deps.server.what().count(IDENTITY) == 2


def test_a_network_that_stays_down_ends_the_run_within_the_wait(fake_deps, monkeypatch):
    fake_deps.server.fail_once[IDENTITY] = dropped()
    original = fake_deps.server.connect

    def down_after_the_drop(target, what):
        if IDENTITY not in fake_deps.server.fail_once and what == "true":
            fake_deps.now += 10  # ssh's connect timeout
            raise SshError("port")
        return original(target, what)

    monkeypatch.setattr(fake_deps.server, "connect", down_after_the_drop)
    started = []
    monkeypatch.setattr(steps, "_wait_for_network", _timed(steps._wait_for_network, fake_deps, started))
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert fake_deps.now - started[0] <= steps.NETWORK_WAIT_SECONDS + 10
    assert "已清理干净" in fake_deps.terminal.text


def test_a_step_that_fails_again_is_not_run_a_third_time(fake_deps):
    fake_deps.server.fail_on[IDENTITY] = dropped()
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert fake_deps.server.what().count(IDENTITY) == 2


def test_a_cloudflare_request_without_an_answer_counts_as_a_drop(fake_deps, monkeypatch):
    original = steps.step_register
    calls = []

    @functools.wraps(original)
    def flaky(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise CloudflareError(0, ["请求超时"])
        original(ctx)

    monkeypatch.setattr(steps, "STEPS", tuple(flaky if s is original else s for s in steps.STEPS))
    assert run_up(fake_deps, aws="jp-tokyo") == 0 and len(calls) == 2


def test_a_record_save_that_drops_is_retried_without_running_the_step_again(fake_deps, monkeypatch):
    original = steps.step_register
    calls, saves = [], []

    @functools.wraps(original)
    def counted(ctx):
        calls.append(1)
        original(ctx)
        save = ctx.save

        def flaky_once():
            saves.append(1)
            if len(saves) == 1:
                raise CloudflareError(0, ["请求超时"])
            save()

        monkeypatch.setattr(ctx, "save", flaky_once)

    monkeypatch.setattr(steps, "STEPS", tuple(counted if s is original else s for s in steps.STEPS))
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(calls) == 1 and len(saves) >= 2


def test_the_first_login_is_not_run_again(fake_deps):
    # step 2 has its own waits and its own diagnosis of an unreachable server
    fake_deps.server.fail_on["true"] = SshError("key")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "连接中断" not in fake_deps.terminal.text


def _timed(function, fake_deps, started):
    def call(*args, **kwargs):
        started.append(fake_deps.now)
        return function(*args, **kwargs)

    return call
