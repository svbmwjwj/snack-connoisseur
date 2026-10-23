"""Where a new alias block goes in ~/.ssh/config, and how two cnsr runs share the file.

OpenSSH takes the first value it finds for each option, so a new block must come before any
block that also applies to the alias (``Host *`` above all, which password-manager guides put
first), or that block's User, Port or IdentityAgent wins for the user's own ``ssh <alias>``.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest
from ssh_config_samples import BLOCK, BLOCK_TEXT

from cnsr.ssh import config

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fcntl is POSIX only")


def written(tmp_path, original: str) -> str:
    p = tmp_path / "config"
    p.write_text(original)
    config.write_block(p, BLOCK)
    return p.read_text()


def test_a_new_block_goes_before_host_star_with_its_comments(tmp_path):
    head = "Include ~/.orbstack/ssh/config\n\nHost prod\n    HostName 192.0.2.5\n\n"
    star = "# 1Password\nHost *\n    IdentityAgent ~/agent.sock\n"
    assert written(tmp_path, head + star) == head + BLOCK_TEXT + "\n" + star


@pytest.mark.parametrize("line", ["Host jp-*", "Host j?-1 other", "Match all", "Host prod jp-1"])
def test_a_new_block_goes_before_any_block_that_applies_to_the_alias(tmp_path, line):
    original = f"{line}\n    User me\n"
    assert written(tmp_path, original) == BLOCK_TEXT + "\n" + original


@pytest.mark.parametrize("line", ["Host *.internal", "Host * !jp-1", "Host prod"])
def test_blocks_that_do_not_apply_to_the_alias_stay_ahead(tmp_path, line):
    original = f"{line}\n    User me\n"
    assert written(tmp_path, original) == original + "\n" + BLOCK_TEXT


@posix_only
def test_a_second_run_waits_for_the_first_to_finish_writing(tmp_path):
    import fcntl

    p = tmp_path / "config"
    p.write_text("Host prod\n    HostName 192.0.2.5\n")
    with open(tmp_path / config.LOCK_NAME, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        writer = threading.Thread(target=config.write_block, args=(p, BLOCK))
        writer.start()
        time.sleep(0.2)
        assert "jp-1" not in p.read_text()  # still waiting for the lock
        # the other run adds its own block meanwhile; the waiting one must not drop it
        p.write_text(p.read_text() + "\nHost other\n    HostName 192.0.2.6\n")
    writer.join(5)
    text = p.read_text()
    assert "Host other" in text and "Host jp-1" in text


def test_the_new_file_reaches_the_disk_before_it_replaces_the_old_one(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(config.os, "fsync", lambda fd: synced.append(fd))
    written(tmp_path, "Host prod\n    HostName 192.0.2.5\n")
    assert synced
