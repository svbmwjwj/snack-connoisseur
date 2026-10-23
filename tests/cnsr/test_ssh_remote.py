"""Tests for running commands on a server over the system OpenSSH client."""

from __future__ import annotations

import dataclasses
import re
import subprocess

import pytest
from ssh_fakes import ADMIN, CONFIG, T, FakeSsh, option_values, run_on

from cnsr.errors import CnsrError
from cnsr.ssh.remote import CommandFailed, Remote, SshError, Target, classify


def run_on_output(stdout: bytes) -> str:
    return Remote(T, CONFIG, run=FakeSsh(stdout=stdout)).run(["true"])


# --- building the ssh command ------------------------------------------------------------


def test_run_builds_safe_args():
    fake = FakeSsh(stdout=b"ok")
    out = Remote(ADMIN, CONFIG, run=fake).run(["echo", "a b"], sudo=True)
    a = fake.args
    assert out == "ok"
    assert a[:3] == ["ssh", "-F", str(CONFIG)]
    assert a[-3:] == ["--", "jp-1", "sudo -n echo 'a b'"]
    assert option_values(a) == [
        "BatchMode=yes",
        "ConnectTimeout=10",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
        "StrictHostKeyChecking=accept-new",
        "ControlMaster=no",
        "ControlPath=none",  # a master the user's own ssh left open is not shared either
        "HostName=192.0.2.10",
        "Port=22",
        "User=admin",
        "IdentitiesOnly=yes",
        "IdentityFile=~/.ssh/pub/jp-1.pub",
    ]
    assert a[3 : a.index("--")] == [x for value in option_values(a) for x in ("-o", value)]


def test_run_never_uses_a_shell_and_sends_bytes():
    fake = FakeSsh()
    Remote(T, CONFIG, run=fake).run(["true"], timeout=42)
    assert not fake.kwargs.get("shell")
    assert fake.kwargs["capture_output"] is True and fake.kwargs["timeout"] == 42
    assert fake.kwargs["stdin"] == subprocess.DEVNULL and "input" not in fake.kwargs
    assert not fake.kwargs.get("text") and not fake.kwargs.get("universal_newlines")
    assert "encoding" not in fake.kwargs


def test_run_sudo_prefix_only_for_a_normal_user():
    assert run_on(T, ["systemctl", "stop", "x"], sudo=True).args[-1] == "systemctl stop x"
    assert run_on(ADMIN, ["id", "-u"]).args[-1] == "id -u"
    assert run_on(ADMIN, ["id", "-u"], sudo=True).args[-1] == "sudo -n id -u"


def test_run_quotes_every_argument_into_one_remote_command():
    fake = run_on(T, ["echo", "$HOME; rm -rf /", "it's"])
    assert fake.args[-1] == "echo '$HOME; rm -rf /' 'it'\"'\"'s'"


def test_options_for_identity_file_and_agent():
    plain = option_values(run_on(Target("jp-1", "192.0.2.10", "root", 2222, None, None)).args)
    assert "Port=2222" in plain
    assert not any(v.startswith(("IdentitiesOnly", "IdentityFile", "IdentityAgent")) for v in plain)
    agent = dataclasses.replace(T, identity_agent="/run/agent.sock")
    assert "IdentityAgent=/run/agent.sock" in option_values(run_on(agent).args)


def test_option_values_with_spaces_are_quoted_for_ssh():
    path = "/Users/u/Library/Group Containers/x/agent.sock"
    target = dataclasses.replace(T, identity_agent=path, identity_file="/home/o'brien/.ssh/pub/jp-1.pub")
    values = option_values(run_on(target).args)
    assert f'IdentityAgent="{path}"' in values
    assert 'IdentityFile="/home/o\'brien/.ssh/pub/jp-1.pub"' in values


@pytest.mark.parametrize("field", ["hostname", "user", "identity_file", "identity_agent"])
@pytest.mark.parametrize("bad", ['a"b', "a\nb", "a\x00b"])
def test_option_values_that_ssh_cannot_carry_are_refused(field, bad):
    fake = FakeSsh()
    with pytest.raises(CnsrError, match="SSH"):
        run_on(dataclasses.replace(T, **{field: bad}), fake=fake)
    assert fake.calls == []


# --- output, errors and timeouts ---------------------------------------------------------


def test_run_decodes_utf8_and_keeps_line_endings():
    assert run_on_output(b"caf\xc3\xa9 \xff") == "caf\u00e9 \ufffd"
    assert run_on_output(b"a\r\nb\n") == "a\r\nb\n"


@pytest.mark.parametrize(
    "stderr,kind",
    [
        ("ssh: connect to host 192.0.2.10 port 22: Connection refused", "port"),
        ("ssh: connect to host 192.0.2.10 port 22: Connection timed out", "port"),
        ("ssh: connect to host 192.0.2.10 port 22: Operation timed out", "port"),
        ("ssh: connect to host 192.0.2.10 port 22: No route to host", "port"),
        ("ssh: Could not resolve hostname jp-1: Name or service not known", "port"),
        ("Permission denied (publickey).", "key"),
        ("root@192.0.2.10: Permission denied (publickey,password).", "key"),
        ("Permission denied, please try again.", "password"),
        ("root@192.0.2.10: Permission denied (password).", "password"),
        ("Permission denied, please try again.\nroot@192.0.2.10: Permission denied (publickey,password).", "password"),
        ("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", "hostkey"),
        ("Host key verification failed.", "hostkey"),
        ("kex_exchange_identification: read: Connection reset by peer", "other"),
        ("", "other"),
    ],
)
def test_classify(stderr, kind):
    assert classify(stderr) == kind


@pytest.mark.parametrize(
    "stderr,kind,hint",
    [
        ("Permission denied (publickey).", "key", "密钥被拒：确认公钥已装到服务器、agent 已解锁"),
        ("Connection refused", "port", "连不上 SSH 端口：检查 IP、端口和厂商防火墙"),
        ("Permission denied, please try again.", "password", "密码错误：cnsr 不会自动重试，确认后重跑"),
        ("Host key verification failed.", "hostkey", "主机指纹不符：如果刚重装过系统，可以删除旧指纹后重试"),
    ],
)
def test_run_raises_ssh_error_on_255(stderr, kind, hint):
    with pytest.raises(SshError) as caught:
        run_on(T, fake=FakeSsh(returncode=255, stderr=stderr.encode()))
    assert caught.value.kind == kind
    assert str(caught.value) == hint
    assert isinstance(caught.value, CnsrError)


@pytest.mark.parametrize(
    "stderr,shown",
    [
        (b"kex_exchange_identification: read: Connection reset by peer\n", "kex_exchange_identification"),
        (b"", "255"),
    ],
)
def test_run_255_with_unknown_cause_shows_what_ssh_said(stderr, shown):
    with pytest.raises(SshError) as caught:
        run_on(T, fake=FakeSsh(returncode=255, stderr=stderr))
    assert caught.value.kind == "other"
    assert "SSH" in str(caught.value) and shown in str(caught.value)


def test_run_raises_cnsr_error_with_exit_code_and_last_stderr_lines():
    stderr = "\n".join(f"line {n}" for n in range(1, 9)) + "\n"
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["false"], fake=FakeSsh(returncode=3, stderr=stderr.encode()))
    message = str(caught.value)
    assert not isinstance(caught.value, SshError)
    assert "3" in message
    assert "line 8" in message and "line 4" in message
    assert "line 3" not in message


def test_run_error_falls_back_to_stdout_when_stderr_says_nothing():
    # xray run -test prints why it failed on stdout and exits 23
    stdout = "Xray 26.3.27 (Xray, Penetrates Everything.)\nFailed to get format of /usr/local/etc/xray/config.json.new\n"
    stderr = "Warning: Permanently added '192.0.2.10' (ED25519) to the list of known hosts.\n"
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["false"], fake=FakeSsh(returncode=23, stdout=stdout.encode(), stderr=stderr.encode()))
    assert "23" in str(caught.value) and "Failed to get format" in str(caught.value)


def test_run_error_prefers_stderr_over_stdout():
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["false"], fake=FakeSsh(returncode=1, stdout=b"progress\n", stderr=b"boom\n"))
    assert "boom" in str(caught.value) and "progress" not in str(caught.value)


def test_run_error_omits_the_known_hosts_notice():
    stderr = "Warning: Permanently added '192.0.2.10' (ED25519) to the list of known hosts.\nboom\n"
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["false"], fake=FakeSsh(returncode=1, stderr=stderr.encode()))
    assert "boom" in str(caught.value) and "Permanently added" not in str(caught.value)


def test_a_command_gets_two_minutes_unless_told_otherwise():
    fake = FakeSsh()
    Remote(T, CONFIG, run=fake).run(["true"])
    assert fake.kwargs["timeout"] == 120


def test_a_timeout_names_the_command_and_its_limit():
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["docker", "compose", "up"], fake=FakeSsh(raises=subprocess.TimeoutExpired(["ssh"], 300)), timeout=300)
    assert "docker compose up" in str(caught.value) and "300 秒" in str(caught.value)


def test_run_turns_a_timeout_into_cnsr_error():
    with pytest.raises(CnsrError) as caught:
        run_on(T, ["sleep", "9"], fake=FakeSsh(raises=subprocess.TimeoutExpired(["ssh"], 5)), timeout=5)
    assert "5" in str(caught.value)
    assert not isinstance(caught.value, subprocess.TimeoutExpired)


def test_a_login_check_that_hangs_is_an_ssh_failure():
    # `true` itself never hangs: the time went on the key exchange or the login.
    with pytest.raises(SshError) as caught:
        run_on(T, ["true"], fake=FakeSsh(raises=subprocess.TimeoutExpired(["ssh"], 30)), timeout=30)
    assert caught.value.kind == "other" and not caught.value.unreachable and "30 秒" in str(caught.value)


def test_can_login_is_false_when_the_login_hangs():
    assert Remote(T, CONFIG, run=FakeSsh(raises=subprocess.TimeoutExpired(["ssh"], 60))).can_login() is False


def test_run_reports_a_missing_ssh_client():
    with pytest.raises(CnsrError, match="ssh"):
        run_on(T, fake=FakeSsh(raises=FileNotFoundError("ssh")))


# --- can_login ---------------------------------------------------------------------------


def test_can_login_true_when_the_command_runs():
    fake = FakeSsh()
    assert Remote(T, CONFIG, run=fake).can_login() is True
    assert fake.args[-1] == "true"


@pytest.mark.parametrize("stderr", ["Permission denied (publickey).", "Connection refused", "weird"])
def test_can_login_false_on_any_ssh_error(stderr):
    assert Remote(T, CONFIG, run=FakeSsh(returncode=255, stderr=stderr.encode())).can_login() is False


def test_a_failed_command_keeps_its_exit_status():
    with pytest.raises(CommandFailed) as caught:
        run_on(T, ["false"], fake=FakeSsh(returncode=3, stderr=b"nothing changed"))
    assert caught.value.exit_code == 3 and "nothing changed" in str(caught.value)


def test_can_login_lets_other_errors_through():
    with pytest.raises(CnsrError):
        Remote(T, CONFIG, run=FakeSsh(returncode=1)).can_login()


# --- the run log ---------------------------------------------------------------------------


def test_run_is_logged_without_output_on_success(caplog):
    caplog.set_level("INFO", logger="cnsr")
    Remote(T, CONFIG, run=FakeSsh(stdout=b"out-1234567")).run(["echo", "hi"])
    text = caplog.text
    assert "192.0.2.10" in text and "echo hi" in text and "退出码 0" in text
    assert re.search(r"退出码 0，耗时 \d+\.\d 秒$", caplog.records[-1].getMessage())
    assert "out-1234567" not in text


def test_failed_run_logs_both_tails(caplog):
    caplog.set_level("INFO", logger="cnsr")
    fake = FakeSsh(returncode=23, stdout=b"Failed to get format\n", stderr=b"boom\n")
    with pytest.raises(CnsrError):
        Remote(T, CONFIG, run=fake).run(["xray", "run", "-test"])
    assert re.search(r"退出码 23，耗时 \d+\.\d 秒\n", caplog.text)
    assert "Failed to get format" in caplog.text and "boom" in caplog.text


def test_input_and_uploaded_content_are_never_logged(caplog):
    caplog.set_level("INFO", logger="cnsr")
    remote = Remote(T, CONFIG, run=FakeSsh())
    remote.run(["chpasswd"], input="admin:pw-9876543")
    remote.put_text("/etc/cnsr/node.env", "content-7654321", "600")
    assert "pw-9876543" not in caplog.text and "content-7654321" not in caplog.text
    (line,) = [r.getMessage() for r in caplog.records if "写入文件" in r.getMessage()]
    assert "/etc/cnsr/node.env" in line and "600" in line


def test_run_script_logs_the_name_and_arguments_not_the_body(caplog):
    caplog.set_level("INFO", logger="cnsr")
    fake = FakeSsh()
    Remote(T, CONFIG, run=fake).run_script("verify-reality.sh", ["/etc/cnsr/verify.env"])
    body = fake.kwargs["input"].decode()
    assert "verify-reality.sh" in caplog.text and "/etc/cnsr/verify.env" in caplog.text
    assert body.splitlines()[-1] not in caplog.text


def test_timeout_is_logged(caplog):
    caplog.set_level("INFO", logger="cnsr")
    fake = FakeSsh(raises=subprocess.TimeoutExpired(["ssh"], 5))
    with pytest.raises(CnsrError):
        Remote(T, CONFIG, run=fake).run(["sleep", "99"], timeout=5)
    assert "sleep 99" in caplog.text and "5 秒" in caplog.text
    assert re.search(r"，耗时 \d+\.\d 秒$", caplog.records[-1].getMessage())
