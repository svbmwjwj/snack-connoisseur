"""Tests for installing a public key with a password and for forgetting host keys."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from ssh_fakes import CONFIG, T, FakeSsh, option_values

from cnsr.errors import CnsrError
from cnsr.ssh.remote import INSTALL_KEY_SCRIPT, SshError, forget_host_key, install_key_with_password


# --- installing a key with a password ----------------------------------------------------

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample jp-1"


def test_install_key_with_password_disables_pubkey():
    fake = FakeSsh()
    install_key_with_password(T, KEY, CONFIG, run=fake)
    a = fake.args
    assert a[:3] == ["ssh", "-F", str(CONFIG)]
    assert a[-3:-1] == ["--", "jp-1"]
    assert a[-1] == shlex.join(["sh", "-c", INSTALL_KEY_SCRIPT])
    assert option_values(a) == [
        "BatchMode=no",
        "ConnectTimeout=10",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
        "StrictHostKeyChecking=accept-new",
        "ControlMaster=no",
        "ControlPath=none",  # a master the user's own ssh left open is not shared either
        "HostName=192.0.2.10",
        "Port=22",
        "User=root",
        "PubkeyAuthentication=no",
        "PreferredAuthentications=password,keyboard-interactive",
    ]
    assert fake.kwargs["input"] == (KEY + "\n").encode()


def test_install_key_with_password_leaves_the_prompt_on_the_terminal():
    fake = FakeSsh()
    install_key_with_password(T, KEY, CONFIG, run=fake)
    assert not fake.kwargs.get("capture_output")
    assert fake.kwargs.get("stdout") is None and fake.kwargs.get("stderr") is None
    assert not fake.kwargs.get("shell")
    assert not fake.kwargs.get("text") and not fake.kwargs.get("universal_newlines")


def test_install_key_with_password_sends_one_clean_line():
    fake = FakeSsh()
    install_key_with_password(T, f"  {KEY}\r\n", CONFIG, run=fake)
    assert fake.kwargs["input"] == (KEY + "\n").encode()


@pytest.mark.parametrize("line", ["", "   \n", f"{KEY}\nssh-rsa AAAB other", f"{KEY}\r{KEY}"])
def test_install_key_with_password_refuses_anything_but_one_key_line(line):
    fake = FakeSsh()
    with pytest.raises(CnsrError):
        install_key_with_password(T, line, CONFIG, run=fake)
    assert fake.calls == []


def test_install_script_guards_against_a_missing_newline_and_a_duplicate():
    script = INSTALL_KEY_SCRIPT
    assert "\n" not in script  # one command line for the remote shell
    assert "umask 077" in script and "mkdir -p ~/.ssh" in script and "touch" in script
    assert "read -r" in script  # the key line comes from stdin
    assert "grep -qxF" in script  # skip a line that is already there
    assert "tail -c 1" in script  # add a newline when the file does not end with one
    assert "cat >>" not in script


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="runs the remote script with a local POSIX sh")


def run_install_script(home: Path, stdin: bytes) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(["sh", "-c", INSTALL_KEY_SCRIPT], input=stdin, env=env, capture_output=True, check=False)


def install_into(home: Path, existing: bytes | None, key: str = KEY) -> bytes:
    """Run the script as if logging in with HOME=``home``; return authorized_keys afterwards."""
    authorized_keys = home / ".ssh" / "authorized_keys"
    if existing is not None:
        authorized_keys.parent.mkdir(mode=0o700)
        authorized_keys.write_bytes(existing)
    done = run_install_script(home, (key + "\n").encode())
    assert done.returncode == 0, done.stderr
    return authorized_keys.read_bytes()


@posix_only
def test_install_script_creates_the_file_with_private_permissions(tmp_path):
    assert install_into(tmp_path, None) == f"{KEY}\n".encode()
    assert (tmp_path / ".ssh").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / ".ssh" / "authorized_keys").stat().st_mode & 0o777 == 0o600


@posix_only
@pytest.mark.parametrize(
    "existing,expected",
    [
        (b"", f"{KEY}\n"),
        (b"ssh-rsa AAAAB3 old", f"ssh-rsa AAAAB3 old\n{KEY}\n"),  # no final newline: do not glue the lines
        (b"ssh-rsa AAAAB3 old\n", f"ssh-rsa AAAAB3 old\n{KEY}\n"),  # final newline: no blank line
        (f"{KEY}-old\n".encode(), f"{KEY}-old\n{KEY}\n"),  # a longer line is not the same key
        (f"x {KEY}\n".encode(), f"x {KEY}\n{KEY}\n"),
    ],
)
def test_install_script_appends_on_a_line_of_its_own(tmp_path, existing, expected):
    assert install_into(tmp_path, existing) == expected.encode()


@posix_only
@pytest.mark.parametrize(
    "existing",
    [f"{KEY}\n", f"ssh-rsa AAAAB3 old\n{KEY}\nssh-rsa AAAAB3 new\n", f"ssh-rsa AAAAB3 old\n{KEY}"],
)
def test_install_script_does_not_add_a_key_twice(tmp_path, existing):
    assert install_into(tmp_path, existing.encode()) == existing.encode()


@posix_only
def test_install_script_rerun_adds_the_key_once(tmp_path):
    expected = f"ssh-rsa AAAAB3 old\n{KEY}\n".encode()
    assert install_into(tmp_path, b"ssh-rsa AAAAB3 old") == expected
    assert run_install_script(tmp_path, f"{KEY}\n".encode()).returncode == 0
    assert (tmp_path / ".ssh" / "authorized_keys").read_bytes() == expected


@posix_only
def test_install_script_fails_without_a_key_line_and_leaves_the_file_alone(tmp_path):
    (tmp_path / ".ssh").mkdir(mode=0o700)
    authorized_keys = tmp_path / ".ssh" / "authorized_keys"
    authorized_keys.write_bytes(b"ssh-rsa AAAAB3 old")
    assert run_install_script(tmp_path, b"").returncode != 0
    assert authorized_keys.read_bytes() == b"ssh-rsa AAAAB3 old"


def test_install_key_with_password_reports_failures():
    with pytest.raises(CnsrError, match="3"):
        install_key_with_password(T, KEY, CONFIG, run=FakeSsh(returncode=3))
    with pytest.raises(SshError) as caught:
        install_key_with_password(T, KEY, CONFIG, run=FakeSsh(returncode=255))
    assert caught.value.kind == "other"
    with pytest.raises(CnsrError, match="ssh"):
        install_key_with_password(T, KEY, CONFIG, run=FakeSsh(raises=FileNotFoundError("ssh")))


# --- forgetting host keys ----------------------------------------------------------------


def test_forget_host_key_non_default_port():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    forget_host_key("192.0.2.10", 2222, run=run)
    assert calls == [["ssh-keygen", "-R", "[192.0.2.10]:2222"]]


@pytest.mark.parametrize(
    "host,port,name",
    [
        ("192.0.2.10", 22, "192.0.2.10"),
        ("2001:db8::1", 22, "2001:db8::1"),
        ("2001:db8::1", 2222, "[2001:db8::1]:2222"),
    ],
)
def test_forget_host_key_names(host, port, name):
    fake = FakeSsh()
    forget_host_key(host, port, run=fake)
    assert fake.args == ["ssh-keygen", "-R", name]


@pytest.mark.parametrize(
    "returncode,stdout,stderr",
    [
        (0, b"Host 192.0.2.10 not found in /home/u/.ssh/known_hosts\n", b""),  # real ssh-keygen exits 0
        (0, b"# Host 192.0.2.10 found: line 1\n/home/u/.ssh/known_hosts updated.\n", b""),
        (255, b"", b"Cannot stat /home/u/.ssh/known_hosts: No such file or directory\n"),
        (255, b"Cannot stat /home/u/.ssh/known_hosts: No such file or directory\n", b""),
    ],
)
def test_forget_host_key_accepts_listed_unlisted_and_missing_known_hosts(returncode, stdout, stderr):
    fake = FakeSsh(returncode=returncode, stdout=stdout, stderr=stderr)
    forget_host_key("192.0.2.10", 22, run=fake)
    assert fake.kwargs["capture_output"] is True and not fake.kwargs.get("shell")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_forget_host_key_raises_on_a_real_failure(stream):
    fake = FakeSsh(returncode=255, **{stream: b"mkstemp: Permission denied\n"})
    with pytest.raises(CnsrError) as caught:
        forget_host_key("192.0.2.10", 22, run=fake)
    assert "255" in str(caught.value) and "Permission denied" in str(caught.value)
    assert "known_hosts" in str(caught.value)


def test_forget_host_key_reports_a_missing_ssh_keygen():
    with pytest.raises(CnsrError, match="ssh-keygen"):
        forget_host_key("192.0.2.10", 22, run=FakeSsh(raises=FileNotFoundError("ssh-keygen")))
