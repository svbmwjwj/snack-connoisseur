"""Tests for config.read_block."""

from __future__ import annotations

from ssh_config_samples import BLOCK, MIXED

from cnsr.ssh import config
from cnsr.ssh.config import HostBlock


def test_read_block_returns_what_write_block_wrote(tmp_path):
    p = tmp_path / "config"
    p.write_text(MIXED)
    config.write_block(p, BLOCK)
    assert config.read_block(p, "jp-1") == BLOCK


def test_read_block_keywords_are_case_insensitive_with_equals(tmp_path):
    p = tmp_path / "config"
    p.write_text(
        "host jp-1\n    HOSTNAME=192.0.2.9\n    user = admin\n    Port\t2222\n"
        "    identityfile ~/.ssh/pub/jp-1.pub\n    IDENTITYAGENT \"/a b/s\"\n"
    )
    assert config.read_block(p, "jp-1") == HostBlock("jp-1", "192.0.2.9", "admin", 2222, "~/.ssh/pub/jp-1.pub", "/a b/s")


def test_read_block_stops_at_next_host_or_match_and_defaults_missing_fields(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    HostName 192.0.2.9\nMatch all\n    User nope\nHost b\n    Port 2200\n")
    assert config.read_block(p, "jp-1") == HostBlock("jp-1", "192.0.2.9", "", 22, "", None)


def test_read_block_ignores_comments_and_inline_comment_on_host_line(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1  # tokyo\n    # User nope\n    HostName 192.0.2.9\n    User admin\n")
    block = config.read_block(p, "jp-1")
    assert (block.hostname, block.user) == ("192.0.2.9", "admin")


def test_read_block_first_value_wins(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    User first\n    User second\n")
    assert config.read_block(p, "jp-1").user == "first"


def test_read_block_invalid_port_falls_back_to_22(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1\n    Port many\n")
    assert config.read_block(p, "jp-1").port == 22


def test_read_block_ignores_multi_pattern_hosts(tmp_path):
    p = tmp_path / "config"
    p.write_text("Host jp-1 other\n    HostName 192.0.2.9\nHost *\n    User x\nHost jp-?\n    User y\nHost !jp-2\n")
    for alias in ("jp-1", "other", "*", "jp-?", "jp-2", "!jp-2"):
        assert config.read_block(p, alias) is None


def test_read_block_missing_file_or_alias(tmp_path):
    p = tmp_path / "config"
    assert config.read_block(p, "jp-1") is None
    p.write_text(MIXED)
    assert config.read_block(p, "jp-2") is None
    assert config.read_block(p, "jp") is None


def test_read_block_tolerates_bom_and_crlf(tmp_path):
    p = tmp_path / "config"
    p.write_bytes(b"\xef\xbb\xbfHost jp-1\r\n    HostName 192.0.2.9\r\n    Port 2200\r\n")
    assert config.read_block(p, "jp-1") == HostBlock("jp-1", "192.0.2.9", "", 2200, "", None)


def test_read_block_does_not_treat_unicode_separators_as_line_ends(tmp_path):
    p = tmp_path / "config"
    p.write_text("# note\u2028Host jp-1\n    HostName 192.0.2.9\n", encoding="utf-8")
    assert config.read_block(p, "jp-1") is None
