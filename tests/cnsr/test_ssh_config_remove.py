"""Tests for config.remove_block."""

from __future__ import annotations

import pytest
from ssh_config_samples import BLOCK, MIXED

from cnsr.ssh import config


def test_remove_block(tmp_path):
    p = tmp_path / "config"
    p.write_text(MIXED)
    assert config.remove_block(p, "jp-1") is True
    assert p.read_text() == "# mine\nHost prod\n    HostName 192.0.2.5\n"
    assert (tmp_path / "config_bak_cnsr").read_text() == MIXED
    assert config.read_block(p, "jp-1") is None


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Host a\n    User a\n\nHost jp-1\n    User b\n\nHost c\n    User c\n", "Host a\n    User a\n\nHost c\n    User c\n"),
        ("Host jp-1\n    User b\n\nHost c\n    User c\n", "Host c\n    User c\n"),
        ("Host jp-1\n    User b\n\n# next\nHost c\n    User c\n", "# next\nHost c\n    User c\n"),
    ],
    ids=["middle", "start", "comment-of-next-block"],
)
def test_remove_block_drops_exactly_one_separator(tmp_path, before, after):
    p = tmp_path / "config"
    p.write_text(before)
    assert config.remove_block(p, "jp-1") is True
    assert p.read_text() == after


def test_remove_block_only_block_leaves_empty_file(tmp_path):
    p = tmp_path / "config"
    config.write_block(p, BLOCK)
    assert config.remove_block(p, "jp-1") is True
    assert p.read_text() == ""


def test_remove_block_missing_alias_or_file_changes_nothing(tmp_path):
    p = tmp_path / "config"
    assert config.remove_block(p, "jp-1") is False
    assert not p.exists()
    p.write_text(MIXED)
    assert config.remove_block(p, "jp-9") is False
    assert p.read_text() == MIXED
    assert not (tmp_path / "config_bak_cnsr").exists()


def test_remove_block_ignores_multi_pattern_and_wildcard_hosts(tmp_path):
    p = tmp_path / "config"
    original = "Host jp-1 other\n    User a\n\nHost *\n    User b\n"
    p.write_text(original)
    assert config.remove_block(p, "jp-1") is False
    assert config.remove_block(p, "*") is False
    assert p.read_text() == original


def test_remove_block_removes_every_duplicate(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    User a\n\nHost b\n    User b\n\nHost jp-1\n    User c\n")
    assert config.remove_block(p, "jp-1") is True
    assert p.read_text() == "Host b\n    User b\n"


def test_remove_block_keeps_other_lines_byte_for_byte(tmp_path):
    p = tmp_path / "config"
    p.write_bytes(b"Host a\r\n\tUser a  \r\n\r\nHost jp-1\r\n    User b\r\n\r\nHost c\r\n    User c\r\n")
    assert config.remove_block(p, "jp-1") is True
    assert p.read_bytes() == b"Host a\r\n\tUser a  \r\n\r\nHost c\r\n    User c\r\n"


def test_repeated_add_and_remove_do_not_accumulate_blank_lines(tmp_path):
    p = tmp_path / "config"
    original = "Host prod\n    HostName 192.0.2.5\n"
    p.write_text(original)
    for _ in range(3):
        config.write_block(p, BLOCK)
        assert config.remove_block(p, "jp-1") is True
        assert p.read_text() == original


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "Host jp-1\n    HostName 192.0.2.9\n\nInclude ~/.ssh/config.d/*\n",
            "Host jp-1\n\nInclude ~/.ssh/config.d/*\n",
        ),
        (
            "Host jp-1\r\n    HostName 192.0.2.9\r\n\r\nInclude ~/.ssh/config.d/*\r\n",
            "Host jp-1\r\n\r\nInclude ~/.ssh/config.d/*\r\n",
        ),
        (
            "Host jp-1\n    hostname=192.0.2.9\n    ServerAliveInterval 30\n    PORT 22\n    IdentitiesOnly yes\n",
            "Host jp-1\n    ServerAliveInterval 30\n",
        ),
        ("Host jp-1\n    # note\n    HostName 192.0.2.9\n", "    # note\n"),
    ],
    ids=["include-after-block", "include-after-block-crlf", "user-directive", "comment-only-rest"],
)
def test_remove_block_removes_only_the_managed_lines(tmp_path, before, after):
    p = tmp_path / "config"
    p.write_bytes(before.encode())
    assert config.remove_block(p, "jp-1") is True
    assert p.read_bytes() == after.encode()


def test_remove_block_keeping_the_host_line_is_not_repeated(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    HostName 192.0.2.9\n    ServerAliveInterval 30\n")
    assert config.remove_block(p, "jp-1") is True
    assert config.remove_block(p, "jp-1") is False
    assert p.read_text() == "Host jp-1\n    ServerAliveInterval 30\n"


TWO_BLOCKS = (
    "Host jp-1\n    HostName 192.0.2.9\n    User admin\n\n"
    "Host jp-1\n    HostName 198.51.100.7\n    User deploy\n    IdentityFile ~/.ssh/work\n"
)
USERS_BLOCK = "Host jp-1\n    HostName 198.51.100.7\n    User deploy\n    IdentityFile ~/.ssh/work\n"


def test_remove_block_limited_to_hostnames_keeps_other_blocks_byte_for_byte(tmp_path):
    p = tmp_path / "config"
    p.write_text(TWO_BLOCKS)
    assert config.remove_block(p, "jp-1", {"192.0.2.9", "edge-cache-v123.example.com"}) is True
    assert p.read_text() == USERS_BLOCK


def test_remove_block_limited_to_hostnames_leaves_a_block_that_points_elsewhere(tmp_path):
    p = tmp_path / "config"
    p.write_text(USERS_BLOCK)
    assert config.remove_block(p, "jp-1", {"192.0.2.9"}) is False
    assert p.read_text() == USERS_BLOCK
    assert not (tmp_path / "config_bak_cnsr").exists()


def test_remove_block_limited_to_hostnames_leaves_a_block_without_hostname(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    ForwardAgent yes\n")
    assert config.remove_block(p, "jp-1", {"192.0.2.9"}) is False
    assert p.read_text() == "Host jp-1\n    ForwardAgent yes\n"


def test_remove_block_with_no_hostnames_to_match_removes_nothing(tmp_path):
    p = tmp_path / "config"
    p.write_text(TWO_BLOCKS)
    assert config.remove_block(p, "jp-1", set()) is False
    assert p.read_text() == TWO_BLOCKS


def test_remove_block_without_a_limit_still_removes_every_duplicate(tmp_path):
    p = tmp_path / "config"
    p.write_text(TWO_BLOCKS)
    assert config.remove_block(p, "jp-1") is True
    assert p.read_text() == ""
