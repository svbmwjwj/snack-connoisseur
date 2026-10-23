"""Tests for what keeps cnsr able to reach a server whose hardening stopped part way.

The new login is reported as soon as a fresh login proved the cut works, a lost connection
while stopping the revert timer is not called an automatic restore, and ``Hardener.resume``
finishes a hardening that an earlier run left between the cut and the final check.
"""

from __future__ import annotations

import logging

import pytest
from harden_fakes import CHECKS, FakeServer, hardener, port_of, sshd_t
from key_samples import ED as PUB

from cnsr.errors import CnsrError
from cnsr.ssh.remote import SshError

PORT = 23456


@pytest.fixture
def fakes() -> FakeServer:
    return FakeServer()


def recorder(fakes: FakeServer):
    """What ``on_cut`` was called with, and the callback, which also marks the moment in ``fakes.log``."""
    recorded: list[tuple[str, int]] = []

    def on_cut(user: str, port: int) -> None:
        recorded.append((user, port))
        fakes.log.append("recorded")

    return recorded, on_cut


# --- the new login is reported once the cut is proven -------------------------------------


def test_the_new_login_is_reported_after_the_fresh_login_and_before_the_timer_stops(fakes):
    recorded, on_cut = recorder(fakes)
    user, port = hardener(fakes).run(PUB, busy_ports={22}, on_cut=on_cut)
    assert recorded == [(user, port)] == [("admin", port)]
    cut = fakes.log.index("cut")
    assert fakes.log[cut:cut + 4] == ["cut", f"login admin:{port}", "recorded", "disarm"]


def test_ctrl_c_while_stopping_the_timer_comes_after_the_report(fakes):
    recorded, on_cut = recorder(fakes)
    fakes.errors["disarm"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        hardener(fakes).run(PUB, busy_ports={22}, on_cut=on_cut)
    assert recorded == [("admin", port_of(fakes))]


@pytest.mark.parametrize("failing", ["cut", "login"])
def test_nothing_is_reported_when_the_cut_is_not_proven(fakes, failing):
    recorded, on_cut = recorder(fakes)
    if failing == "login":
        fakes.login_ok = [True, False]
    else:
        fakes.failing = {"cut"}
    with pytest.raises(CnsrError, match="3 分钟内自动恢复"):
        hardener(fakes).run(PUB, busy_ports={22}, on_cut=on_cut)
    assert recorded == []


# --- a lost connection while stopping the timer ------------------------------------------


def test_a_lost_connection_while_stopping_the_timer_is_not_called_a_restore(fakes):
    fakes.errors["disarm"] = SshError("other", "SSH 连接失败（退出码 255）：\nConnection reset")
    with pytest.raises(CnsrError) as raised:
        hardener(fakes).run(PUB, busy_ports={22})
    message, port = str(raised.value), port_of(fakes)
    assert "自动恢复为" not in message.split("也可能没有")[0]
    assert "可能已经停止" in message and "也可能没有" in message
    assert f"用户 admin、端口 {port}" in message
    assert "控制台" in message and "控制台密码" in message
    assert fakes.slept == [] and not any(step.startswith("revert") for step in fakes.log)
    assert fakes.password not in message


def test_a_failed_timer_stop_that_is_not_an_ssh_failure_still_waits_for_the_timer(fakes):
    fakes.failing = {"disarm"}
    with pytest.raises(CnsrError, match="3 分钟内自动恢复"):
        hardener(fakes).run(PUB, busy_ports={22})
    assert fakes.slept == [200]


# --- resume: finishing a hardening an earlier run left unfinished -------------------------


def test_resume_of_a_finished_hardening_whose_timer_is_gone_changes_nothing(fakes):
    assert fakes.timer == "unloaded"  # stopped by the run that hardened it: `systemctl stop` exits 5
    assert hardener(fakes).resume(PORT) is False
    assert fakes.made == [("admin", PORT)]
    assert fakes.log == ["disarm", "timer state", *CHECKS]
    assert all((call.user, call.port, call.sudo) == ("admin", PORT, True) for call in fakes.calls)


def test_resume_stops_a_timer_an_interrupted_run_left_pending(fakes):
    fakes.timer = "active"
    assert hardener(fakes).resume(PORT) is False
    assert fakes.timer == "unloaded" and fakes.log == ["disarm", "timer state", *CHECKS]


def test_resume_changes_nothing_when_the_timer_cannot_be_stopped(fakes):
    fakes.timer, fakes.timer_stop_fails = "active", True
    with pytest.raises(CnsrError, match="没能停止") as raised:
        hardener(fakes).resume(PORT)
    assert fakes.log == ["disarm", "timer state"]  # no settings read, no cut
    assert "没有做任何改动" in str(raised.value) and "重跑" in str(raised.value)


def test_resume_of_a_staged_server_cuts_again_with_a_new_timer(fakes, caplog):
    caplog.set_level(logging.INFO, logger="cnsr")
    state = {"ports": [22, PORT]}
    fakes.sshd_t = lambda port, *options: sshd_t(state["ports"], passwordauthentication="yes")

    def cut_done(name, remote, sudo, detail=None):
        FakeServer.step(fakes, name, remote, sudo, detail)
        if name == "cut":
            state["ports"] = [PORT]
            fakes.sshd_t = lambda port, *options: sshd_t(state["ports"])

    fakes.step = cut_done  # the timer already fired, so it is gone (`systemctl stop` exits 5)
    assert hardener(fakes).resume(PORT) is True
    cut = fakes.log.index("cut")
    assert fakes.log[:2] == ["disarm", "timer state"]
    # a new timer armed by the cut, a fresh login, then that timer stopped
    assert fakes.log[cut:cut + 3] == ["cut", f"login admin:{PORT}", "disarm"] and fakes.timer == "unloaded"
    assert fakes.call("cut").detail == [str(PORT)]
    assert "stage" not in fakes.log and "chpasswd" not in fakes.log
    assert "补完" in caplog.text and "补完" not in fakes.out.getvalue()  # hardening progress is only logged


def test_resume_that_cannot_read_the_settings_stops(fakes):
    fakes.sshd_t = lambda port, *options: (_ for _ in ()).throw(CnsrError("sshd 出错"))
    with pytest.raises(CnsrError, match="sshd 出错"):
        hardener(fakes).resume(PORT)
    assert "cut" not in fakes.log
