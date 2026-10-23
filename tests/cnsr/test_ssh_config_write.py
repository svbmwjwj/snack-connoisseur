"""Tests for config.write_block."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

import pytest
from ssh_config_samples import BLOCK, BLOCK_TEXT, MIXED

from cnsr.errors import CnsrError
from cnsr.ssh import config
from cnsr.ssh.config import HostBlock

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_block_preserves_other_blocks(tmp_path):
    p = tmp_path / "config"
    p.write_text(MIXED)
    config.write_block(p, BLOCK)
    text = p.read_text()
    assert text.startswith("# mine\nHost prod\n    HostName 192.0.2.5\n")
    assert '    IdentityAgent "/a b/agent.sock"\n' in text and "192.0.2.9" not in text
    assert (tmp_path / "config_bak_cnsr").read_text().count("192.0.2.9") == 1


def test_write_block_replaces_in_place_and_keeps_user_directives(tmp_path):
    p = tmp_path / "config"
    p.write_text(
        "Host a\n    HostName 192.0.2.1\n\n"
        "Host jp-1\n    HostName 192.0.2.9\n    ServerAliveInterval 30\n\n"
        "# the third server\nHost c\n    HostName 192.0.2.3\n"
    )
    config.write_block(p, BLOCK)
    assert p.read_text() == (
        "Host a\n    HostName 192.0.2.1\n\n"
        + BLOCK_TEXT
        + "    ServerAliveInterval 30\n\n# the third server\nHost c\n    HostName 192.0.2.3\n"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "Host jp-1\n    HostName 192.0.2.9\n\nInclude ~/.ssh/config.d/*\n",
            BLOCK_TEXT + "\nInclude ~/.ssh/config.d/*\n",
        ),
        (
            "Host jp-1\r\n    HostName 192.0.2.9\r\n\r\nInclude ~/.ssh/config.d/*\r\n",
            "Host jp-1\r\n" + BLOCK_TEXT.split("\n", 1)[1] + "\r\nInclude ~/.ssh/config.d/*\r\n",
        ),
        (
            "Host jp-1\n    ProxyJump bastion\n    HostName 192.0.2.9\n    # keep me\nCompression yes\n",
            "Host jp-1\n    ProxyJump bastion\n" + BLOCK_TEXT.split("\n", 1)[1] + "    # keep me\nCompression yes\n",
        ),
    ],
    ids=["include-after-block", "include-after-block-crlf", "other-directives-and-comments"],
)
def test_write_block_keeps_every_line_it_does_not_manage(tmp_path, before, after):
    p = tmp_path / "config"
    p.write_bytes(before.encode())
    config.write_block(p, BLOCK)
    assert p.read_bytes() == after.encode()


def test_write_block_keeps_managed_lines_that_already_have_the_wanted_value(tmp_path):
    p = tmp_path / "config"
    original = (
        "Host jp-1\r\n\thostname=edge-cache-v123.example.com\r\n\tUSER admin # login\r\n"
        "\tPort 22\r\n\tIdentityFile ~/.ssh/pub/jp-1.pub\r\n\tidentitiesonly YES\r\n"
        '\tIdentityAgent "/a b/agent.sock"\r\n'
    )
    p.write_bytes(original.encode())
    config.write_block(p, BLOCK)
    assert p.read_bytes() == original.encode()
    assert not (tmp_path / "config_bak_cnsr").exists()


def test_write_block_updates_changed_values_in_place(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    Port 22\n    ServerAliveInterval 30\n    User root\n")
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "admin", 2222, "~/.ssh/pub/jp-1.pub", None))
    assert p.read_text() == (
        "Host jp-1\n    Port 2222\n    ServerAliveInterval 30\n    User admin\n"
        "    HostName 192.0.2.9\n    IdentityFile ~/.ssh/pub/jp-1.pub\n    IdentitiesOnly yes\n"
    )


def test_write_block_inserts_missing_lines_after_the_last_managed_line(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    Port 22\n    ServerAliveInterval 30\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == (
        "Host jp-1\n    Port 22\n"
        "    HostName edge-cache-v123.example.com\n    User admin\n    IdentityFile ~/.ssh/pub/jp-1.pub\n"
        '    IdentitiesOnly yes\n    IdentityAgent "/a b/agent.sock"\n'
        "    ServerAliveInterval 30\n"
    )


def test_write_block_inserts_after_the_host_line_when_nothing_is_managed(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    ServerAliveInterval 30\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT + "    ServerAliveInterval 30\n"


def test_write_block_adds_the_newline_a_last_managed_line_lacked(tmp_path):
    p = tmp_path / "config"
    p.write_bytes(b"Host jp-1\n    HostName edge-cache-v123.example.com")
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT


def test_write_block_drops_managed_lines_that_are_no_longer_wanted(tmp_path):
    p = tmp_path / "config"
    config.write_block(p, BLOCK)
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "", 22, "~/.ssh/pub/jp-1.pub", None))
    assert p.read_text() == (
        "Host jp-1\n    HostName 192.0.2.9\n    Port 22\n    IdentityFile ~/.ssh/pub/jp-1.pub\n"
        "    IdentitiesOnly yes\n"
    )


def test_write_block_drops_repeated_managed_lines(tmp_path):
    p = tmp_path / "config"
    p.write_text(
        "Host jp-1\n    IdentityFile ~/.ssh/id_old\n    IdentityFile ~/.ssh/pub/jp-1.pub\n    HostName 192.0.2.9\n"
    )
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "", 22, "~/.ssh/pub/jp-1.pub", None))
    assert "id_old" not in p.read_text()
    assert p.read_text().count("IdentityFile") == 1
    assert config.read_block(p, "jp-1").identity_file == "~/.ssh/pub/jp-1.pub"


def test_write_block_replaces_block_that_ends_at_match(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    HostName 192.0.2.9\nMatch host *.example.com\n    User x\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT + "Match host *.example.com\n    User x\n"


def test_write_block_replaces_last_block_and_keeps_trailing_blank_lines(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    HostName 192.0.2.9\n\n\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT + "\n\n"


def test_write_block_matches_alias_case_insensitive_keyword_and_equals(tmp_path):
    p = tmp_path / "config"
    p.write_text("HOST=jp-1\n    hostname 192.0.2.9\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == "HOST=jp-1\n" + BLOCK_TEXT.split("\n", 1)[1]


def test_write_block_leaves_multi_pattern_and_wildcard_hosts_alone(tmp_path):
    p = tmp_path / "config"
    original = "Host jp-1 other\n    HostName 192.0.2.7\n\nHost *\n    ServerAliveInterval 30\n"
    p.write_text(original)
    config.write_block(p, BLOCK)
    # untouched; the new block goes before the first one that also applies to jp-1
    assert p.read_text() == BLOCK_TEXT + "\n" + original


def test_write_block_replaces_only_the_first_duplicate(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    HostName 192.0.2.1\n\nHost jp-1\n    HostName 192.0.2.2\n")
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT + "\nHost jp-1\n    HostName 192.0.2.2\n"


def test_write_block_creates_missing_file(tmp_path):
    p = tmp_path / ".ssh" / "config"
    config.write_block(p, BLOCK)
    assert p.read_text() == BLOCK_TEXT
    assert not (tmp_path / ".ssh" / "config_bak_cnsr").exists()


@posix_only
def test_write_block_uses_private_modes(tmp_path):
    p = tmp_path / ".ssh" / "config"
    config.write_block(p, BLOCK)
    assert mode(p.parent) == 0o700
    assert mode(p) == 0o600


@posix_only
def test_write_block_tightens_existing_file_mode_and_keeps_directory(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o755)
    ssh_dir.chmod(0o755)
    p = ssh_dir / "config"
    p.write_text(MIXED)
    p.chmod(0o644)
    config.write_block(p, BLOCK)
    assert mode(p) == 0o600
    assert mode(ssh_dir) == 0o755
    assert mode(ssh_dir / "config_bak_cnsr") == 0o600


@posix_only
def test_write_block_keeps_a_symlinked_config(tmp_path):
    real = tmp_path / "dotfiles" / "ssh-config"
    real.parent.mkdir()
    real.write_text(MIXED)
    link = tmp_path / ".ssh" / "config"
    link.parent.mkdir()
    link.symlink_to(real)
    config.write_block(link, BLOCK)
    assert link.is_symlink()
    assert real.read_text().endswith(BLOCK_TEXT) and "192.0.2.9" not in real.read_text()
    assert (link.parent / "config_bak_cnsr").read_text() == MIXED


@pytest.mark.parametrize(
    ("before", "separator"),
    [
        (b"Host prod\n    HostName 192.0.2.5\n", b"\n"),
        (b"Host prod\n    HostName 192.0.2.5", b"\n"),
        (b"Host prod\n    HostName 192.0.2.5\n\n", b""),
        (b"Host prod\r\n    HostName 192.0.2.5\r\n", b"\n"),
        (b"", b""),
    ],
    ids=["plain", "no-final-newline", "already-blank", "crlf", "empty"],
)
def test_write_block_appends_after_exactly_one_blank_line(tmp_path, before, separator):
    p = tmp_path / "config"
    p.write_bytes(before)
    config.write_block(p, BLOCK)
    kept = before if before.endswith(b"\n") or not before else before + b"\n"
    assert p.read_bytes() == kept + separator + BLOCK_TEXT.encode()


def test_write_block_keeps_untouched_lines_byte_for_byte(tmp_path):
    p = tmp_path / "config"
    before = b"\xef\xbb\xbf# caf\xc3\xa9 \xe9\r\nHost prod\r\n\tHostName 192.0.2.5  \r\n\r\n"
    after = b"\r\nHost other\r\n    User x\r\n"
    p.write_bytes(before + b"Host jp-1\r\n    HostName 192.0.2.9\r\n" + after)
    config.write_block(p, BLOCK)
    # The Host line is not cnsr's to rewrite, so it keeps its CRLF; the lines cnsr writes are LF.
    assert p.read_bytes() == before + b"Host jp-1\r\n" + BLOCK_TEXT.split("\n", 1)[1].encode() + after


def test_write_block_omits_empty_user_and_missing_agent(tmp_path):
    p = tmp_path / "config"
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "", 2222, "~/.ssh/pub/jp-1.pub", None))
    assert p.read_text() == (
        "Host jp-1\n    HostName 192.0.2.9\n    Port 2222\n"
        "    IdentityFile ~/.ssh/pub/jp-1.pub\n    IdentitiesOnly yes\n"
    )


@pytest.mark.parametrize("key", ["C:/Users/A B/.ssh/pub/jp-1.pub", "/home/O'Brien/.ssh/pub/jp-1.pub"])
def test_write_block_quotes_identity_file_with_spaces_or_single_quotes(tmp_path, key):
    p = tmp_path / "config"
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "root", 22, key, None))
    assert f'    IdentityFile "{key}"\n' in p.read_text()
    assert config.read_block(p, "jp-1").identity_file == key


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not installed")
@pytest.mark.parametrize("home", ["/home/O'Brien", "C:/Users/O'Brien"])
def test_ssh_accepts_a_generated_config_with_single_quotes_in_paths(tmp_path, home):
    p = tmp_path / "config"
    p.write_text(MIXED)
    key, agent = f"{home}/.ssh/pub/jp-1.pub", f"{home}/agent dir/agent.sock"
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "admin", 2222, key, agent))

    def ssh_g(host: str) -> subprocess.CompletedProcess:
        args = ["ssh", "-o", "ControlMaster=no", "-G", "-F", str(p), host]
        return subprocess.run(args, capture_output=True, text=True, timeout=30)

    result = ssh_g("jp-1")
    assert result.returncode == 0, result.stderr
    for line in ("hostname 192.0.2.9", "user admin", "port 2222", f"identityfile {key}", f"identityagent {agent}"):
        assert line in result.stdout.splitlines()
    other = ssh_g("other-host")
    assert other.returncode == 0, other.stderr
    assert "hostname other-host" in other.stdout.splitlines()


def test_write_block_backup_is_rolling_and_only_for_existing_files(tmp_path):
    p = tmp_path / "config"
    bak = tmp_path / "config_bak_cnsr"
    config.write_block(p, BLOCK)
    assert not bak.exists()
    first = p.read_bytes()
    config._backed_up.clear()  # a later cnsr run
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "root", 22, "~/.ssh/pub/jp-1.pub", None))
    assert bak.read_bytes() == first
    second = p.read_bytes()
    config._backed_up.clear()
    config.write_block(p, BLOCK)
    assert bak.read_bytes() == second


def test_write_block_backup_holds_the_file_from_before_this_run(tmp_path):
    p = tmp_path / "config"
    bak = tmp_path / "config_bak_cnsr"
    p.write_text("Host other\n    Port 2200\n")
    original = p.read_bytes()
    config.write_block(p, BLOCK)
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "root", 22, "~/.ssh/pub/jp-1.pub", None))
    config.remove_block(p, "jp-1")
    assert bak.read_bytes() == original


def test_a_file_cnsr_created_in_this_run_does_not_replace_an_older_backup(tmp_path):
    p = tmp_path / "config"
    bak = tmp_path / "config_bak_cnsr"
    bak.write_bytes(b"# from an earlier run\n")
    config.write_block(p, BLOCK)
    config.write_block(p, HostBlock("jp-1", "192.0.2.9", "root", 22, "~/.ssh/pub/jp-1.pub", None))
    assert bak.read_bytes() == b"# from an earlier run\n"


def test_write_block_backup_is_the_exact_original(tmp_path):
    p = tmp_path / "config"
    original = b"\xef\xbb\xbfHost jp-1\r\n    HostName 192.0.2.9\r\n\xff\r\n"
    p.write_bytes(original)
    config.write_block(p, BLOCK)
    assert (tmp_path / "config_bak_cnsr").read_bytes() == original


def test_write_block_without_changes_keeps_the_backup(tmp_path):
    p = tmp_path / "config"
    p.write_text(MIXED)
    config.write_block(p, BLOCK)
    bak = tmp_path / "config_bak_cnsr"
    assert bak.read_text() == MIXED
    config.write_block(p, BLOCK)
    assert bak.read_text() == MIXED


@pytest.mark.parametrize(
    "block",
    [
        HostBlock("jp 1", "192.0.2.9", "root", 22, "k.pub", None),
        HostBlock("jp-*", "192.0.2.9", "root", 22, "k.pub", None),
        HostBlock("", "192.0.2.9", "root", 22, "k.pub", None),
        HostBlock("jp-1", "", "root", 22, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9\nProxyCommand evil", "root", 22, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "ro ot", 22, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "root", 0, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "root", 65536, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "", None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "k.pub\nProxyCommand evil", None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "k.pub", '/a"\nProxyCommand evil'),
        HostBlock("o'b", "192.0.2.9", "root", 22, "k.pub", None),
        HostBlock("jp-1", "o'b.example.com", "root", 22, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "o'b", 22, "k.pub", None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, 'C:/a"b/k.pub', None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "C:\\keys\\", None),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "k.pub", '/a"b/agent.sock'),
        HostBlock("jp-1", "192.0.2.9", "root", 22, "k.pub", "C:\\agents\\"),
    ],
)
def test_write_block_rejects_values_that_would_corrupt_the_config(tmp_path, block):
    p = tmp_path / "config"
    p.write_text(MIXED)
    with pytest.raises(CnsrError):
        config.write_block(p, block)
    assert p.read_text() == MIXED
    assert not (tmp_path / "config_bak_cnsr").exists()


def test_write_block_failure_leaves_no_temp_files(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text(MIXED)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(CnsrError):
        config.write_block(p, BLOCK)
    assert p.read_text() == MIXED
    assert sorted(x.name for x in tmp_path.iterdir()) == ["config", config.LOCK_NAME]  # the lock stays by design
