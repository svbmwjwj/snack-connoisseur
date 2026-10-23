"""cnsr-rotate.sh: a fault of the node itself is not blamed on the backup being tried."""

from __future__ import annotations

import sys

import pytest
from rotate_harness import OLD_SNI, node  # noqa: F401  (fixture)
from test_rotate_script import A, B, rotating

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")


def kept_and_reported(node) -> None:
    assert node.backup_domains() == ["a.example.com", "b.example.com"]
    assert node.reality()["serverNames"] == [OLD_SNI]
    assert [e["kind"] for e in node.sent("POST", "event")] == ["rotate_failed"]


def test_a_backup_that_fails_while_the_node_is_offline_is_kept(node):
    rotating(node, A, B)
    node.offline()
    node.run("once")
    kept_and_reported(node)
    assert "连不上外网" in node.sent("POST", "event")[0]["detail"]


def test_a_self_check_that_fails_while_the_node_is_offline_keeps_the_backup(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.self_check_fails("a.example.com", "b.example.com")
    node.offline()
    node.run("once")
    kept_and_reported(node)


def test_a_self_check_that_cannot_run_keeps_the_backup(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.fail("verify.rc", 2)
    node.run("once")
    kept_and_reported(node)
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI]]  # tried once, then rolled back


def test_a_backup_that_fails_while_the_node_is_online_is_still_dropped(node):
    rotating(node, A, B)
    node.valid("b.example.com")
    node.run("once")
    assert node.reality()["serverNames"] == ["b.example.com"]
    assert node.backup_domains() == []
