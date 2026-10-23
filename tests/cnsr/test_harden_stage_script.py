"""Tests for harden-stage.sh, run against a fake server (see harden_harness)."""

from __future__ import annotations

import base64
import sys

import pytest
from harden_harness import NODE, harden_tool, server  # noqa: F401  (fixtures)
from key_samples import ED as PUB
from key_samples import RSA

from cnsr.server.harden import stage_args

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

PORT = 23456
PACKAGES = ("ufw", "fail2ban", "python3-systemd", "unattended-upgrades", "sudo")
APT = "apt-get -qq -o DPkg::Lock::Timeout=120"
REVERT = "/usr/local/sbin/cnsr-harden-revert"
DROP_IN = "/etc/ssh/sshd_config.d/10-cnsr.conf"
KEYS = "/home/admin/.ssh/authorized_keys"
SSHD = [
    "sshd -t",
    "sshd -T",
    "systemctl is-active --quiet ssh.socket",
    "systemctl restart ssh",
]
UFW_MARKER = "/opt/cnsr/.ufw-enabled-by-cnsr"
FIREWALL = [
    "ufw status",
    "ufw allow 22/tcp",
    f"ufw allow {PORT}/tcp",
    "ufw allow 443/tcp",
    "ufw allow 443/udp",
    "ufw default deny incoming",
    "ufw --force enable",
]
FAIL2BAN = ["systemctl enable --quiet fail2ban", "systemctl restart fail2ban"]
FRESH = [
    *[f"dpkg-query -W -f=${{Status}} {package}" for package in PACKAGES],
    f"{APT} update",
    f"{APT} -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y {' '.join(PACKAGES)}",
    "id admin",
    "getent group admin",
    "useradd -m -d /home/admin -s /bin/bash admin",
    "chown -R admin: /home/admin/.ssh",
    "visudo -cf /etc/sudoers.d/90-cnsr-admin.cnsr-new",
    *SSHD,
    *FIREWALL,
    *FAIL2BAN,
]


def stage(server, port: int = PORT, pubkey: str = PUB):
    return server.run("harden-stage.sh", *stage_args(port, pubkey))


@pytest.fixture
def fresh(server):
    """A fresh server whose sshd reads the drop-in directory."""
    server.answer("sshd -T", f"port 22\nport {PORT}\npasswordauthentication yes\n")
    return server


def installed(server) -> None:
    for package in PACKAGES:
        server.answer(f"dpkg-query -W -f=${{Status}} {package}", "install ok installed")
    server.answer("id admin", status=0)


# --- a fresh server ----------------------------------------------------------------------


def test_stage_on_a_fresh_server_runs_every_step_in_order(fresh):
    out = stage(fresh)
    assert (out.status, out.stderr) == (0, "")
    assert out.calls == FRESH
    assert not out.stdin_leaked
    assert fresh.leftovers() == []


def test_stage_installs_exactly_harden_revert_sh_as_the_revert_program(fresh):
    stage(fresh)
    assert fresh.path(REVERT).read_bytes() == (NODE / "harden-revert.sh").read_bytes()
    assert fresh.mode(REVERT) == 0o755


def test_stage_listens_on_both_ports(fresh):
    stage(fresh)
    assert fresh.read(DROP_IN) == f"Port 22\nPort {PORT}\n"
    assert fresh.mode(DROP_IN) == 0o644


def test_stage_gives_admin_the_key_and_passwordless_sudo(fresh):
    stage(fresh)
    assert fresh.read(KEYS) == PUB + "\n"
    assert (fresh.mode("/home/admin/.ssh"), fresh.mode(KEYS)) == (0o700, 0o600)
    assert fresh.read("/etc/sudoers.d/90-cnsr-admin") == "admin ALL=(ALL) NOPASSWD:ALL\n"
    assert fresh.mode("/etc/sudoers.d/90-cnsr-admin") == 0o440


def test_stage_writes_fail2ban_and_automatic_updates(fresh):
    stage(fresh)
    assert fresh.read("/etc/fail2ban/jail.d/cnsr.local") == f"[sshd]\nenabled = true\nport = {PORT}\nbackend = systemd\n"
    assert fresh.read("/etc/apt/apt.conf.d/20auto-upgrades") == (
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )


def test_ufw_allows_22_and_the_new_port_before_it_is_enabled(fresh):
    calls = stage(fresh).calls
    enable = calls.index("ufw --force enable")
    assert calls.index("ufw allow 22/tcp") < calls.index("ufw default deny incoming") < enable
    assert calls.index(f"ufw allow {PORT}/tcp") < enable


def test_a_ufw_that_was_off_is_marked_as_enabled_by_cnsr(fresh):
    fresh.answer("ufw status", "Status: inactive\n")
    assert stage(fresh).status == 0
    assert fresh.path(UFW_MARKER).exists()


def test_a_ufw_that_was_already_on_is_not_marked(fresh):
    fresh.answer("ufw status", "Status: active\n\nTo Action From\n-- ------ ----\n22/tcp ALLOW Anywhere\n")
    assert stage(fresh).status == 0
    assert not fresh.path(UFW_MARKER).exists()


def test_ssh_restarts_through_the_socket_when_it_is_socket_activated(fresh):
    fresh.answer("systemctl is-active --quiet ssh.socket", status=0)
    calls = stage(fresh).calls
    assert "systemctl daemon-reload" in calls and "systemctl restart ssh.socket" in calls
    assert "systemctl restart ssh" not in calls


def test_an_existing_admin_group_is_used(fresh):
    fresh.answer("getent group admin", status=0)
    assert "useradd -m -d /home/admin -s /bin/bash -g admin admin" in stage(fresh).calls


# --- running it again --------------------------------------------------------------------


def test_stage_twice_installs_and_adds_nothing_the_second_time(fresh):
    stage(fresh)
    installed(fresh)
    out = stage(fresh)
    assert out.status == 0
    assert not any(call.startswith(("apt-get", "useradd")) for call in out.calls)
    assert fresh.read(KEYS) == PUB + "\n"
    assert fresh.read(DROP_IN) == f"Port 22\nPort {PORT}\n"


def test_a_key_file_without_a_final_newline_keeps_its_last_key_whole(fresh):
    fresh.write(KEYS, RSA, mode=0o600)
    assert stage(fresh).status == 0
    assert fresh.read(KEYS) == f"{RSA}\n{PUB}\n"


def test_a_rerun_with_another_port_replaces_the_drop_in(fresh):
    fresh.write(DROP_IN, "Port 22\nPort 30000\n")
    assert stage(fresh).status == 0
    assert fresh.read(DROP_IN) == f"Port 22\nPort {PORT}\n"


# --- failures ----------------------------------------------------------------------------


def test_a_rejected_drop_in_is_put_back_and_ssh_is_not_restarted(fresh):
    fresh.write(DROP_IN, "Port 22\nPort 30000\n")
    fresh.answer("sshd -t", status=1)
    out = stage(fresh)
    assert out.status == 1 and "sshd -t" in out.stderr
    assert fresh.read(DROP_IN) == "Port 22\nPort 30000\n"
    assert out.calls[-1] == "sshd -t"
    assert fresh.leftovers() == []


def test_a_rejected_new_drop_in_is_removed(fresh):
    fresh.answer("sshd -t", status=1)
    assert stage(fresh).status == 1
    assert not fresh.path(DROP_IN).exists()


def test_stage_stops_when_sshd_ignores_the_drop_in_directory(fresh):
    fresh.answer("sshd -T", "port 22\n")
    out = stage(fresh)
    assert out.status == 1 and "sshd_config.d" in out.stderr
    assert out.calls[-1] == "sshd -T"


def test_stage_never_exits_255(fresh):
    fresh.answer("sshd -t", status=255)
    assert stage(fresh).status == 1


def test_a_failed_package_install_stops_before_any_user_or_ssh_change(fresh):
    fresh.answer(f"{APT} update", status=100)
    out = stage(fresh)
    assert out.status == 100
    assert out.calls[-1] == f"{APT} update"
    assert fresh.path(REVERT).exists()
    assert not fresh.path(DROP_IN).exists()


def test_a_sudo_rule_that_visudo_rejects_is_not_installed(fresh):
    fresh.answer("visudo -cf /etc/sudoers.d/90-cnsr-admin.cnsr-new", status=1)
    out = stage(fresh)
    assert out.status == 1 and "visudo" in out.stderr
    assert not fresh.path("/etc/sudoers.d/90-cnsr-admin").exists()
    assert fresh.leftovers() == []


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    "args",
    [
        ("22x", PUB, b64("true\n")),
        ("", PUB, b64("true\n")),
        ("80", PUB, b64("true\n")),
        ("123456", PUB, b64("true\n")),
        (str(PORT), "", b64("true\n")),
        (str(PORT), PUB + "\nssh-ed25519 AAAA other", b64("true\n")),
        (str(PORT), PUB + "\r", b64("true\n")),
        (str(PORT), PUB, ""),
        (str(PORT), PUB, b64("if then\n")),
        (str(PORT), PUB, "!!not-base64"),
        (str(PORT), PUB),
    ],
)
def test_bad_arguments_change_nothing(fresh, args):
    out = fresh.run("harden-stage.sh", *args)
    assert out.status == 1 and out.stderr
    assert out.calls == []
    assert not fresh.path(REVERT).exists()
    assert fresh.leftovers() == []
