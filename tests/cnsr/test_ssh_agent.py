"""Tests for agent detection, key listing and public key files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cnsr.errors import CnsrError
from cnsr.ssh import agent
from cnsr.ssh.agent import AgentKey

HOME = Path("/home/u")
MAC_BITWARDEN = "/home/u/Library/Containers/com.bitwarden.desktop/Data/.bitwarden-ssh-agent.sock"
MAC_1PASSWORD = "/home/u/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
MAC_BITWARDEN_DMG = "/home/u/.bitwarden-ssh-agent.sock"  # the build from bitwarden.com, not the App Store
LINUX_BITWARDEN = "/home/u/.bitwarden-ssh-agent.sock"
LINUX_1PASSWORD = "/home/u/.1password/agent.sock"


def existing(*paths: str):
    return lambda path: path in paths


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return lambda args, **kw: subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_detect_agents_macos_prefers_bitwarden():
    found = agent.detect_agents(
        {"SSH_AUTH_SOCK": "/tmp/x"},
        HOME,
        "darwin",
        exists=existing("/tmp/x", MAC_1PASSWORD, MAC_BITWARDEN),
    )
    assert found == [MAC_BITWARDEN, MAC_1PASSWORD, "/tmp/x"]


def test_detect_agents_macos_finds_the_bitwarden_build_from_its_website():
    found = agent.detect_agents({}, HOME, "darwin", exists=existing(MAC_BITWARDEN_DMG, MAC_1PASSWORD))
    assert found == [MAC_BITWARDEN_DMG, MAC_1PASSWORD]


def test_detect_agents_linux_paths():
    found = agent.detect_agents(
        {"SSH_AUTH_SOCK": "/tmp/x"},
        HOME,
        "linux",
        exists=existing("/tmp/x", LINUX_1PASSWORD, LINUX_BITWARDEN),
    )
    assert found == [LINUX_BITWARDEN, LINUX_1PASSWORD, "/tmp/x"]


def test_detect_agents_keeps_only_existing_paths():
    found = agent.detect_agents(
        {"SSH_AUTH_SOCK": "/tmp/gone"}, HOME, "linux", exists=existing(LINUX_1PASSWORD)
    )
    assert found == [LINUX_1PASSWORD]


def test_detect_agents_removes_duplicates():
    found = agent.detect_agents(
        {"SSH_AUTH_SOCK": LINUX_1PASSWORD}, HOME, "linux", exists=existing(LINUX_1PASSWORD)
    )
    assert found == [LINUX_1PASSWORD]


def test_detect_agents_ignores_unset_and_empty_auth_sock():
    assert agent.detect_agents({}, HOME, "linux", exists=lambda path: False) == []
    checked: list[str] = []
    agent.detect_agents({"SSH_AUTH_SOCK": ""}, HOME, "linux", exists=lambda p: checked.append(p) or False)
    assert "" not in checked


def test_detect_agents_does_not_use_other_platform_paths():
    found = agent.detect_agents({}, HOME, "linux", exists=existing(MAC_BITWARDEN, MAC_1PASSWORD))
    assert found == []


def test_detect_agents_windows_uses_default_pipe():
    assert agent.detect_agents({}, Path("C:/Users/a"), "win32") == [None]


def test_detect_agents_checks_real_paths_by_default(tmp_path):
    socket_path = tmp_path / "agent.sock"
    socket_path.touch()
    assert agent.detect_agents({"SSH_AUTH_SOCK": str(socket_path)}, tmp_path, "linux") == [str(socket_path)]


def test_list_keys_parses_comment():
    run = completed("ssh-ed25519 AAAA jp-1\nssh-rsa BBBB other key\n")
    keys = agent.list_keys("/s", run=run)
    assert [k.comment for k in keys] == ["jp-1", "other key"]
    assert [k.line for k in keys] == ["ssh-ed25519 AAAA jp-1", "ssh-rsa BBBB other key"]


def test_list_keys_key_without_comment():
    keys = agent.list_keys("/s", run=completed("ssh-ed25519 AAAA\n"))
    assert keys == [AgentKey("ssh-ed25519 AAAA", "")]


def test_list_keys_tolerates_crlf_and_blank_lines():
    keys = agent.list_keys("/s", run=completed("ssh-ed25519 AAAA jp-1\r\n\r\necdsa-sha2-nistp256 CCCC b\r\n"))
    assert [k.comment for k in keys] == ["jp-1", "b"]
    assert keys[0].line == "ssh-ed25519 AAAA jp-1"


def test_list_keys_skips_lines_that_are_not_keys():
    keys = agent.list_keys("/s", run=completed("The agent has no identities.\nsk-ssh-ed25519@openssh.com DDDD hw\n"))
    assert [k.comment for k in keys] == ["hw"]


def test_list_keys_runs_ssh_add_with_the_agent_socket(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/other")
    monkeypatch.setenv("CNSR_MARK", "kept")
    calls: list[tuple[list[str], dict]] = []

    def run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "", "")

    agent.list_keys("/s", run=run)
    args, kw = calls[0]
    assert args == ["ssh-add", "-L"]
    assert kw["env"]["SSH_AUTH_SOCK"] == "/s"
    assert kw["env"]["CNSR_MARK"] == "kept"
    assert (kw["encoding"], kw["errors"], kw["timeout"]) == ("utf-8", "replace", agent.SSH_ADD_TIMEOUT)


def test_list_keys_agent_that_does_not_answer_is_a_chinese_error():
    def run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    with pytest.raises(CnsrError) as err:
        agent.list_keys("/run/agent.sock", run=run)
    assert "/run/agent.sock" in str(err.value) and "没有回应" in str(err.value)


def test_list_keys_default_agent_keeps_the_environment(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/inherited")
    seen: list[dict] = []

    def run(args, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    agent.list_keys(None, run=run)
    assert seen[0]["SSH_AUTH_SOCK"] == "/inherited"


def test_list_keys_no_identities_is_empty():
    assert agent.list_keys("/s", run=completed("The agent has no identities.\n", returncode=1)) == []


def test_list_keys_unreachable_agent_names_the_agent():
    with pytest.raises(CnsrError) as err:
        agent.list_keys("/run/agent.sock", run=completed("", returncode=2, stderr="Error connecting to agent"))
    assert "/run/agent.sock" in str(err.value)


def test_list_keys_unexpected_exit_code_is_an_error():
    with pytest.raises(CnsrError) as err:
        agent.list_keys("/s", run=completed("", returncode=255))
    assert "/s" in str(err.value) and "255" in str(err.value)


def test_list_keys_missing_ssh_add_names_the_agent():
    def run(args, **kw):
        raise FileNotFoundError("ssh-add")

    with pytest.raises(CnsrError) as err:
        agent.list_keys("/run/agent.sock", run=run)
    assert "/run/agent.sock" in str(err.value) and "ssh-add" in str(err.value)


def test_list_keys_default_agent_has_a_readable_name():
    with pytest.raises(CnsrError) as err:
        agent.list_keys(None, run=completed("", returncode=2))
    assert "None" not in str(err.value)


def test_find_key_matches_comment_exactly():
    keys = [AgentKey("ssh-ed25519 A jp-10", "jp-10"), AgentKey("ssh-ed25519 B jp-1", "jp-1")]
    assert agent.find_key(keys, "jp-1") is keys[1]
    assert agent.find_key(keys, "jp") is None
    assert agent.find_key(keys, "JP-1") is None
    assert agent.find_key([], "jp-1") is None


def test_ensure_pub_file_writes_key_line_with_lf(tmp_path):
    key = AgentKey("ssh-ed25519 AAAA jp-1", "jp-1")
    pub_dir = tmp_path / ".ssh" / "pub"
    path = agent.ensure_pub_file(key, "jp-1", pub_dir)
    assert path == pub_dir / "jp-1.pub"
    assert path.read_bytes() == b"ssh-ed25519 AAAA jp-1\n"


def test_ensure_pub_file_leaves_existing_file_alone(tmp_path):
    existing_file = tmp_path / "jp-1.pub"
    existing_file.write_bytes(b"ssh-ed25519 OLD jp-1\r\n")
    path = agent.ensure_pub_file(AgentKey("ssh-ed25519 NEW jp-1", "jp-1"), "jp-1", tmp_path)
    assert path == existing_file
    assert existing_file.read_bytes() == b"ssh-ed25519 OLD jp-1\r\n"


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "a\\b"])
def test_ensure_pub_file_rejects_names_that_escape_the_directory(tmp_path, name):
    with pytest.raises(CnsrError):
        agent.ensure_pub_file(AgentKey("ssh-ed25519 A c", "c"), name, tmp_path / "pub")
    assert not (tmp_path / "pub").exists()


@pytest.mark.parametrize(
    "platform,expected",
    [("darwin", [MAC_BITWARDEN, MAC_BITWARDEN_DMG, MAC_1PASSWORD]), ("linux", [LINUX_BITWARDEN, LINUX_1PASSWORD]), ("win32", [])],
)
def test_known_sockets(platform, expected):
    assert agent.known_sockets(HOME, platform) == expected


def test_agent_socket_reads_the_session_variable_for_the_sentinel():
    env = {"SSH_AUTH_SOCK": "/tmp/launchd/Listeners"}
    assert agent.agent_socket(agent.SESSION_AGENT, env) == "/tmp/launchd/Listeners"
    assert agent.agent_socket(agent.SESSION_AGENT, {}) is None
    assert agent.agent_socket(LINUX_BITWARDEN, env) == LINUX_BITWARDEN
    assert agent.agent_socket(None, env) is None
