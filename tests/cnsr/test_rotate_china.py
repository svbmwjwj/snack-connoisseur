"""cnsr-rotate.sh: a backup is checked from mainland China (china-check.sh) before it is used."""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest
from rotate_harness import OLD_SNI, node  # noqa: F401  (fixture)
from test_rotate_script import A, B, rotating

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")


def test_a_backup_blocked_in_china_is_dropped_before_xray_is_touched(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.china("a.example.com", "blocked")
    result = node.run("once")
    assert result.status == 0
    assert any(call.startswith("china-check.sh a.example.com 203.0.113.10 ") for call in result.calls)
    assert node.reality()["serverNames"] == ["b.example.com"]
    assert node.jsonl("restarts.jsonl") == [["b.example.com"]]  # a.example.com never reached Xray
    assert "从国内握手失败" in result.stderr


def test_a_backup_china_could_not_judge_goes_by_the_node_alone(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.china("a.example.com", "unknown")
    result = node.run("once")
    assert node.reality()["serverNames"] == ["a.example.com"]
    assert "没能从国内检验" in result.stderr


def test_china_is_asked_only_about_backups_the_node_accepted(node):
    rotating(node, A, B)
    node.valid("b.example.com")
    result = node.run("once")
    [call] = [c for c in result.calls if c.startswith("china-check.sh")]
    assert call.startswith("china-check.sh b.example.com 203.0.113.11 ")


def test_china_is_given_what_is_left_of_the_time(node):
    node.env["CNSR_CHINA_BUDGET_SECONDS"] = "40"
    rotating(node, A)
    node.valid("a.example.com")
    result = node.run("once")
    [call] = [c for c in result.calls if c.startswith("china-check.sh")]
    assert 0 < int(call.rsplit(" ", 1)[1]) <= 40


def test_a_node_without_the_check_goes_by_the_node_alone(node):
    (node.root / "bin" / "china-check.sh").unlink()  # installed before this version
    rotating(node, A)
    node.valid("a.example.com")
    assert node.run("once").status == 0
    assert node.reality()["serverNames"] == ["a.example.com"]


def test_every_backup_blocked_in_china_is_reported_as_exhausted(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.china("a.example.com", "blocked")
    node.china("b.example.com", "blocked")
    node.run("once")
    assert node.backup_domains() == []
    assert [event["kind"] for event in node.sent("POST", "event")] == ["exhausted"]


def test_a_stop_during_the_china_check_puts_the_backup_back(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    (node.base / "china-blocks").touch()
    process = node.start()
    deadline = time.monotonic() + 20
    while not (node.base / "china-started").exists():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    subprocess.run(["pkill", "-TERM", "-P", str(process.pid)], check=False)
    assert process.wait(timeout=20) == 143
    assert node.backup_domains() == ["a.example.com", "b.example.com"]
    assert node.reality()["serverNames"] == [OLD_SNI]


def test_china_checks_stop_once_their_time_is_used_up(node):
    node.env["CNSR_CHINA_BUDGET_SECONDS"] = "0"  # already used up
    rotating(node, A)
    node.valid("a.example.com")
    node.china("a.example.com", "blocked")
    result = node.run("once")
    assert not any(call.startswith("china-check.sh") for call in result.calls)
    assert node.reality()["serverNames"] == ["a.example.com"]
    assert "国内检验已用完" in result.stderr


def test_an_empty_list_is_reported_as_used_up(node):
    rotating(node)
    node.run("once")
    assert [e["detail"] for e in node.sent("POST", "event")] == ["备选 SNI 已用完，保持旧配置"]


def test_backups_that_all_failed_are_reported_as_such(node):
    rotating(node, A)
    node.run("once")  # a.example.com does not validate
    assert [e["detail"] for e in node.sent("POST", "event")] == ["备选 SNI 都没通过复验，保持旧配置"]
