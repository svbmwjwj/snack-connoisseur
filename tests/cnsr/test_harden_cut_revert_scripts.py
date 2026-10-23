"""Tests for harden-cut.sh, harden-revert.sh and baseline-check.sh, run against a fake server
(see harden_harness)."""

from __future__ import annotations

import sys

import pytest
from harden_harness import NODE, harden_tool, server  # noqa: F401  (fixtures)

from cnsr.server.baseline import parse_contexts, per_login

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

PORT = 23456
REVERT = "/usr/local/sbin/cnsr-harden-revert"
DROP_IN = "/etc/ssh/sshd_config.d/10-cnsr.conf"
JAIL = "/etc/fail2ban/jail.d/cnsr.local"
UFW_MARKER = "/opt/cnsr/.ufw-enabled-by-cnsr"
STAGED = f"Port 22\nPort {PORT}\n"
CUT = f"Port {PORT}\nPermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
ARM = [
    "systemctl stop cnsr-ssh-revert.timer cnsr-ssh-revert.service",
    "systemctl reset-failed cnsr-ssh-revert.timer cnsr-ssh-revert.service",
    f"systemd-run --unit=cnsr-ssh-revert --on-active=180 --timer-property=AccuracySec=1s {REVERT} stage",
]
RESTART = ["systemctl is-active --quiet ssh.socket", "systemctl restart ssh"]
SOCKET_RESTART = ["systemctl is-active --quiet ssh.socket", "systemctl daemon-reload", "systemctl restart ssh.socket"]


@pytest.fixture
def staged(server):
    """A server after harden-stage.sh: the revert program is installed and sshd has both ports."""
    server.write(REVERT, (NODE / "harden-revert.sh").read_bytes(), mode=0o755)
    server.write(DROP_IN, STAGED)
    return server


def cut(server, port: str = str(PORT)):
    return server.run("harden-cut.sh", port)


def revert(server, mode: str = "stage"):
    return server.run("harden-revert.sh", mode)


# --- harden-cut.sh -----------------------------------------------------------------------


def test_cut_arms_the_timer_before_it_touches_ssh_or_the_firewall(staged):
    out = cut(staged)
    assert (out.status, out.stderr) == (0, "")
    assert out.calls == [*ARM, "sshd -t", "ufw delete allow 22/tcp", *RESTART]
    assert staged.read(DROP_IN) == CUT
    assert not out.stdin_leaked
    assert staged.leftovers() == []


def test_cut_changes_nothing_when_the_timer_cannot_be_armed(staged):
    staged.answer(ARM[-1], status=1)
    out = cut(staged)
    assert out.status == 3  # changed nothing: cnsr need not wait for a timer
    assert out.calls == ARM
    assert staged.read(DROP_IN) == STAGED


def test_cut_changes_nothing_without_the_revert_program(staged):
    staged.path(REVERT).unlink()
    out = cut(staged)
    assert out.status == 3 and REVERT in out.stderr
    assert out.calls == []
    assert staged.read(DROP_IN) == STAGED


def test_cut_clears_a_stale_timer_even_when_there_is_none(staged):
    staged.answer(ARM[0], status=5)
    staged.answer(ARM[1], status=1)
    assert cut(staged).status == 0


def test_cut_puts_the_staged_drop_in_back_when_sshd_rejects_it(staged):
    staged.answer("sshd -t", status=1)
    out = cut(staged)
    assert out.status == 1
    assert out.calls == [*ARM, "sshd -t"]
    assert staged.read(DROP_IN) == STAGED
    assert staged.leftovers() == []


def test_cut_never_exits_255(staged):
    staged.answer("sshd -t", status=255)
    assert cut(staged).status == 1


def test_cut_restarts_through_the_socket_when_it_is_socket_activated(staged):
    staged.answer("systemctl is-active --quiet ssh.socket", status=0)
    assert cut(staged).calls[-3:] == SOCKET_RESTART


@pytest.mark.parametrize("port", ["", "22x", "80", "123456"])
def test_cut_refuses_a_bad_port_before_anything(staged, port):
    out = cut(staged, port)
    assert out.status == 3 and out.calls == []


# --- harden-revert.sh stage (what the timer runs) ----------------------------------------


def test_revert_stage_reopens_22_next_to_the_new_port(staged):
    staged.write(DROP_IN, CUT)
    out = revert(staged)
    assert (out.status, out.stderr) == (0, "")
    assert out.calls == ["ufw allow 22/tcp", f"ufw allow {PORT}/tcp", "sshd -t", *RESTART]
    assert staged.read(DROP_IN) == STAGED
    assert not out.stdin_leaked
    assert staged.leftovers() == []


def test_revert_stage_of_a_staged_server_is_a_no_op_for_ssh(staged):
    assert revert(staged).status == 0
    assert staged.read(DROP_IN) == STAGED


def test_revert_stage_without_a_drop_in_still_opens_22(staged):
    staged.path(DROP_IN).unlink()
    out = revert(staged)
    assert out.status == 0 and out.calls[0] == "ufw allow 22/tcp"
    assert staged.read(DROP_IN) == "Port 22\n"


def test_revert_falls_back_to_the_stock_ssh_config_when_sshd_rejects_the_drop_in(staged):
    staged.write(DROP_IN, CUT)
    staged.answer("sshd -t", status="1 0")
    out = revert(staged)
    assert out.status == 1 and "sshd" in out.stderr
    assert out.calls[-4:] == ["sshd -t", "sshd -t", *RESTART]
    assert not staged.path(DROP_IN).exists()


def test_revert_never_restarts_into_a_config_sshd_rejects(staged):
    staged.write(DROP_IN, CUT)
    staged.answer("sshd -t", status=1)
    out = revert(staged)
    assert out.status == 1
    assert not any(call.startswith("systemctl") for call in out.calls)


def test_revert_keeps_going_when_ufw_fails(staged):
    staged.write(DROP_IN, CUT)
    staged.answer("ufw allow 22/tcp", status=1)
    out = revert(staged)
    assert out.status == 1
    assert out.calls[-2:] == RESTART
    assert staged.read(DROP_IN) == STAGED


def test_revert_never_exits_255(staged):
    staged.answer("sshd -t", status=255)
    assert revert(staged).status == 1


def test_revert_restarts_through_the_socket_when_it_is_socket_activated(staged):
    staged.answer("systemctl is-active --quiet ssh.socket", status=0)
    assert revert(staged).calls[-3:] == SOCKET_RESTART


@pytest.mark.parametrize("mode", ["", "everything"])
def test_revert_refuses_an_unknown_mode(staged, mode):
    out = revert(staged, mode)
    assert out.status == 2 and out.calls == []
    assert staged.read(DROP_IN) == STAGED


# --- harden-revert.sh all (rollback before the cut) --------------------------------------


def test_revert_all_undoes_the_staging(staged):
    staged.write(JAIL, "[sshd]\n")
    staged.write(UFW_MARKER, "")
    out = revert(staged, "all")
    assert (out.status, out.stderr) == (0, "")
    assert out.calls == [
        f"ufw delete allow {PORT}/tcp",
        "ufw disable",
        "sshd -t",
        *RESTART,
        "systemctl try-restart fail2ban",
    ]
    assert not staged.path(DROP_IN).exists() and not staged.path(JAIL).exists()
    assert not staged.path(UFW_MARKER).exists()


def test_revert_all_keeps_a_ufw_that_cnsr_did_not_enable(staged):
    out = revert(staged, "all")
    assert out.status == 0
    assert f"ufw delete allow {PORT}/tcp" in out.calls and "ufw disable" not in out.calls


def test_revert_all_keeps_the_marker_when_ufw_cannot_be_disabled(staged):
    staged.write(UFW_MARKER, "")
    staged.answer("ufw disable", status=1)
    assert revert(staged, "all").status == 1
    assert staged.path(UFW_MARKER).exists()  # a retry still knows that cnsr enabled it


def test_revert_all_after_a_stage_that_stopped_early(staged):
    staged.path(DROP_IN).unlink()
    staged.remove_tool("ufw")
    out = revert(staged, "all")
    assert out.status == 0
    assert out.calls == ["sshd -t", *RESTART]


def test_revert_all_only_warns_when_fail2ban_does_not_restart(staged):
    staged.write(JAIL, "[sshd]\n")
    staged.answer("systemctl try-restart fail2ban", status=1)
    out = revert(staged, "all")
    assert out.status == 0 and "fail2ban" in out.stderr


# --- baseline-check.sh -------------------------------------------------------------------

SSHD_T = "port 22\npermitrootlogin without-password\npasswordauthentication no\nkbdinteractiveauthentication no\n"
ROOT_SSHD_T = SSHD_T.replace("passwordauthentication no", "passwordauthentication yes")  # a Match User root block
APT = "apt-get -qq -o DPkg::Lock::Timeout=120"
ADMIN_CHECK = "sshd -T -C user=admin,host=cnsr,addr=192.0.2.1"
ROOT_CHECK = "sshd -T -C user=root,host=cnsr,addr=192.0.2.1"


def test_baseline_check_turns_on_updates_and_prints_only_sshd_t_for_each_login(server):
    server.answer(ADMIN_CHECK, SSHD_T)
    server.answer(ROOT_CHECK, ROOT_SSHD_T)
    server.answer(f"{APT} update", "Hit:1 http://deb.example.com bookworm InRelease\n")
    out = server.run("baseline-check.sh")
    assert (out.status, out.stdout) == (0, f"cnsr-context admin\n{SSHD_T}cnsr-context root\n{ROOT_SSHD_T}")
    assert out.calls == [
        "dpkg-query -W -f=${Status} unattended-upgrades",
        f"{APT} update",
        f"{APT} -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y unattended-upgrades",
        "systemctl enable --quiet --now unattended-upgrades",
        ADMIN_CHECK,
        ROOT_CHECK,
    ]
    assert per_login(parse_contexts(out.stdout)) == ["SSH 允许密码登录（root 登录时）"]
    assert server.read("/etc/apt/apt.conf.d/20auto-upgrades") == (
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )
    assert not out.stdin_leaked


def test_baseline_check_skips_apt_when_unattended_upgrades_is_installed(server):
    server.answer("dpkg-query -W -f=${Status} unattended-upgrades", "install ok installed")
    assert not any(call.startswith("apt-get") for call in server.run("baseline-check.sh").calls)


@pytest.mark.parametrize("check", [ADMIN_CHECK, ROOT_CHECK])
def test_baseline_check_never_exits_255(server, check):
    server.answer(check, status=255)
    assert server.run("baseline-check.sh").status == 1
