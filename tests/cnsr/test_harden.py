"""Tests for hardening a small-vendor server: the order of the steps, rollback and the timed revert.

``FakeServer`` (see ``harden_fakes``) stands in for both sides of one server: the root login cnsr
already has and the admin logins on the new port. It names every step in ``log``.
"""

from __future__ import annotations

import base64
import random
from importlib.resources import files

import pytest
from harden_fakes import ADMIN_CHECK, CHECKS, FakeServer, hardener, port_of, sshd_t
from key_samples import ED as PUB

from cnsr.errors import CnsrError
from cnsr.ssh.remote import CommandFailed
from cnsr.server.harden import UNCHANGED, PASSWORD_ALPHABET, console_password, pick_ssh_port, stage_args
from cnsr.server.xray import PORTS

NODE = files("cnsr.assets.node")


@pytest.fixture
def fakes() -> FakeServer:
    return FakeServer()


# --- the happy path ----------------------------------------------------------------------


def test_hardener_happy_path_order(fakes):
    user, port = hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert user == "admin" and 20000 <= port <= 59999
    assert fakes.log == ["stage", f"login admin:{port}", "confirm 已保存", "chpasswd", "cut", f"login admin:{port}", "disarm", *CHECKS]
    assert fakes.slept == []


def test_stage_runs_as_root_with_the_revert_program(fakes):
    _, port = hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    stage = fakes.call("stage")
    assert (stage.user, stage.port) == ("root", 22)
    assert stage.detail == stage_args(port, PUB, PORTS)
    assert stage.detail[:2] == [str(port), PUB]
    # Sent with LF line endings whatever the checkout has, like every streamed script.
    revert = NODE.joinpath("harden-revert.sh").read_bytes().replace(b"\r\n", b"\n")
    assert base64.b64decode(stage.detail[2], validate=True) == revert


def test_everything_after_stage_runs_as_admin_on_the_new_port_with_sudo(fakes):
    _, port = hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.made == [("admin", port)]
    assert [(c.step, c.user, c.port, c.sudo) for c in fakes.calls[1:]] == [
        (f"login admin:{port}", "admin", port, True),
        ("chpasswd", "admin", port, True),
        ("cut", "admin", port, True),
        (f"login admin:{port}", "admin", port, True),
        ("disarm", "admin", port, True),
        (CHECKS[0], "admin", port, True),
        (CHECKS[1], "admin", port, True),
    ]
    assert fakes.call("cut").detail == [str(port)]


def test_trailing_whitespace_of_the_key_is_dropped(fakes):
    _, port = hardener(fakes).run(PUB + "\n", busy_ports={22}, app_ports=PORTS)
    assert fakes.call("stage").detail[:2] == [str(port), PUB]


def test_new_port_avoids_busy_ports(fakes):
    first = pick_ssh_port(random.Random(1), set())
    _, port = hardener(fakes).run(PUB, busy_ports={22, first}, app_ports=PORTS)
    assert port != first and 20000 <= port <= 59999


# --- the console password ----------------------------------------------------------------


def test_console_password_is_set_after_it_was_shown_and_saved(fakes):
    hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    password = fakes.password
    assert fakes.call("chpasswd").detail == f"admin:{password}\n"
    assert len(password) == 20 and set(password) <= set(PASSWORD_ALPHABET)
    assert password in fakes.shown_before_prompt[0]


def test_console_password_is_shown_exactly_once_and_nowhere_else(fakes):
    hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    password = fakes.password
    assert fakes.out.getvalue().count(password) == 1
    assert not any(password in str(call.detail) for call in fakes.calls if call.step != "chpasswd")


def test_the_prompt_waits_for_the_exact_answer(fakes):
    fakes.answers = ["好了", "已保存"]
    hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.log.count("confirm 已保存") == 1 and "chpasswd" in fakes.log
    assert "请原样输入“已保存”" in fakes.out.getvalue()


# --- failures before the cut: roll everything back ---------------------------------------


def test_reverts_all_when_new_login_fails_before_cut(fakes):
    fakes.login_ok = [False]
    with pytest.raises(CnsrError, match="已回滚"):
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert "revert all" in fakes.log and "cut" not in fakes.log


@pytest.mark.parametrize("failing", ["stage", "login", "confirm", "chpasswd"])
def test_any_failure_before_the_cut_rolls_back_as_root(fakes, failing):
    if failing == "login":
        fakes.login_ok = [False]
    elif failing == "confirm":
        fakes.answers = [EOFError()]
    else:
        fakes.failing = {failing}
    with pytest.raises(CnsrError, match="已回滚") as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.log[-1] == "revert all" and "cut" not in fakes.log
    revert = fakes.call("revert all")
    assert (revert.user, revert.port, revert.detail) == ("root", 22, ["all"])
    assert fakes.slept == []
    if "chpasswd" in fakes.log:
        assert fakes.password not in str(raised.value)


def test_a_failed_new_login_says_which_port(fakes):
    fakes.login_ok = [False]
    with pytest.raises(CnsrError) as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    port = port_of(fakes)
    assert f"新端口 {port}" in str(raised.value) and "厂商防火墙" in str(raised.value)


def test_a_failed_rollback_is_not_reported_as_done(fakes):
    fakes.failing = {"stage", "revert all"}
    with pytest.raises(CnsrError) as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    message = str(raised.value)
    assert "已回滚" not in message
    assert "回滚也失败" in message and "/usr/local/sbin/cnsr-harden-revert all" in message


def test_interrupt_before_the_cut_leaves_the_staged_server_alone(fakes):
    fakes.answers = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.log == ["stage", f"login admin:{port_of(fakes)}"]


def test_a_multiline_key_is_refused_before_touching_the_server(fakes):
    with pytest.raises(CnsrError, match="一行"):
        hardener(fakes).run(PUB + "\nssh-ed25519 AAAA other", busy_ports={22}, app_ports=PORTS)
    assert fakes.log == []


# --- failures after the cut: the timer brings the server back ----------------------------


def test_waits_for_timer_when_login_fails_after_cut(fakes):
    fakes.login_ok = [True, False]
    with pytest.raises(CnsrError, match="3 分钟内自动恢复"):
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert "disarm" not in fakes.log


@pytest.mark.parametrize("failing", ["cut", "login", "disarm"])
def test_any_failure_after_the_cut_waits_for_the_timer_and_never_rolls_back_by_hand(fakes, failing):
    if failing == "login":
        fakes.login_ok = [True, False]
    else:
        fakes.failing = {failing}
    with pytest.raises(CnsrError, match="3 分钟内自动恢复") as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.slept == [200]
    assert not any(step.startswith("revert") for step in fakes.log)
    assert not set(CHECKS) & set(fakes.log)
    assert "3 分钟内自动恢复" in fakes.out.getvalue()
    assert fakes.password not in str(raised.value)


def test_a_cut_that_changed_nothing_does_not_wait_for_a_timer_it_never_armed(fakes):
    fakes.errors["cut"] = CommandFailed(UNCHANGED, "远程命令执行失败（退出码 3）：\n没能启动自动恢复计时器")
    with pytest.raises(CnsrError, match="没能启动自动恢复计时器") as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.slept == [] and "3 分钟内自动恢复" not in str(raised.value)
    assert not any(step.startswith("revert") for step in fakes.log)


# --- the final check ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "ports", "expected"),
    [
        ({}, "both", "SSH 端口应只有"),
        ({"passwordauthentication": "yes"}, "new", "SSH 允许密码登录"),
        ({"kbdinteractiveauthentication": "yes"}, "new", "SSH 允许键盘交互登录"),
        ({"permitrootlogin": "prohibit-password"}, "new", "SSH 仍允许 root 登录"),
    ],
)
def test_a_failed_final_check_restores_the_staged_state(fakes, settings, ports, expected):
    fakes.sshd_t = lambda port, *options: sshd_t([22, port] if ports == "both" else [port], **settings)
    with pytest.raises(CnsrError, match=expected) as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    port = port_of(fakes)
    assert fakes.log[-3:] == [*CHECKS, "revert stage"]
    revert = fakes.call("revert stage")
    assert (revert.user, revert.port, revert.sudo) == ("admin", port, True)
    assert "22 端口和新端口都开" in str(raised.value)
    assert fakes.slept == []


def test_a_failed_restore_after_the_final_check_says_how_to_log_in(fakes):
    fakes.sshd_t = lambda port, *options: sshd_t([port], passwordauthentication="yes")
    fakes.failing = {"revert stage"}
    with pytest.raises(CnsrError) as raised:
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert f"admin 和端口 {port_of(fakes)}" in str(raised.value)


def test_a_match_block_that_lets_root_in_with_a_password_is_caught(fakes):
    def settings(port, *options):
        return sshd_t([port], permitrootlogin="yes" if "user=root" in options[-1] else "no")

    fakes.sshd_t = settings
    with pytest.raises(CnsrError, match="root 用密码登录（root 登录时）"):
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.log[-1] == "revert stage"


def test_an_unreadable_final_check_also_restores(fakes):
    fakes.failing = {ADMIN_CHECK}
    with pytest.raises(CnsrError, match="读取 SSH 设置失败"):
        hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.log[-1] == "revert stage"


# --- the server script order (the text is what the timer protects) -----------------------


def test_cut_arms_revert_timer_before_touching_ports():
    text = files("cnsr.assets.node").joinpath("harden-cut.sh").read_text()
    assert text.index("systemd-run --unit=cnsr-ssh-revert --on-active=180") < text.index("10-cnsr.conf") < text.index("ufw delete allow 22/tcp")


def test_cut_names_the_installed_revert_program_in_its_timer():
    text = files("cnsr.assets.node").joinpath("harden-cut.sh").read_text()
    line = "systemd-run --unit=cnsr-ssh-revert --on-active=180 --timer-property=AccuracySec=1s /usr/local/sbin/cnsr-harden-revert stage ||\n"
    assert line in text


def test_cut_timer_fires_on_time():
    # Without it systemd may delay the timer by up to a minute (AccuracySec defaults to 1min),
    # past the 200 seconds cnsr waits before telling you to rerun.
    text = files("cnsr.assets.node").joinpath("harden-cut.sh").read_text()
    timer = next(line for line in text.splitlines() if line.strip().startswith("systemd-run "))
    assert "--timer-property=AccuracySec=1s" in timer.split()


# --- ports and passwords -----------------------------------------------------------------


def test_pick_ssh_port_stays_in_range():
    ports = {pick_ssh_port(random.Random(seed), set()) for seed in range(200)}
    assert all(20000 <= port <= 59999 for port in ports) and len(ports) > 150


def test_pick_ssh_port_avoids_busy():
    assert pick_ssh_port(random.Random(1), set(range(20000, 60000)) - {31337}) == 31337


def test_pick_ssh_port_is_reproducible_with_the_same_rng():
    assert pick_ssh_port(random.Random(7), {22}) == pick_ssh_port(random.Random(7), {22})


def test_pick_ssh_port_refuses_when_every_port_is_busy():
    with pytest.raises(CnsrError):
        pick_ssh_port(random.Random(1), set(range(20000, 60000)))


def test_console_password_alphabet():
    rng = random.Random(3)
    passwords = [console_password(rng) for _ in range(50)]
    assert all(len(password) == 20 for password in passwords)
    assert set("".join(passwords)) <= set(PASSWORD_ALPHABET)
    assert not set("".join(passwords)) & set("0O1lI")
    assert len(set(passwords)) == 50


def test_password_alphabet_is_the_documented_one():
    assert PASSWORD_ALPHABET == "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def test_console_password_is_registered(fakes, monkeypatch):
    from cnsr import runlog

    registered = []
    monkeypatch.setattr(runlog, "secret", registered.append)
    hardener(fakes).run(PUB, busy_ports={22}, app_ports=PORTS)
    assert fakes.password in registered
