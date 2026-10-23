"""cnsr-rotate.sh against a control plane that lags: versions on task and line, and short 401 spells."""

from __future__ import annotations

import sys
import time

import pytest
from rotate_harness import ALIAS, node  # noqa: F401  (fixture)
from test_rotate_script import NO_ROTATE, A, B, rotating

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

LINE_A = '{"sni":"a.example.com","dest_ip":"203.0.113.10"}'


def task_urls(node) -> list[str]:
    return [r["url"].split(f"/node/{ALIAS}/", 1)[1] for r in node.requests() if r["key"].endswith("/task")]


def test_an_accepted_line_records_its_version(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.respond("PUT", "line", [200, '{"version":7}'])
    assert node.run("once").status == 0
    assert node.read("last-version") == "7\n" and node.mode("last-version") == 0o600


def test_a_pending_line_written_later_records_its_version(node):
    node.write("line-pending.json", LINE_A + "\n")
    node.write("last-version", "3\n")
    node.respond("PUT", "line", [200, '{"version":4}'])
    node.respond("GET", "task", NO_ROTATE)
    node.run("once")
    assert node.read("last-version") == "4\n"
    # the task asked right after carries it
    assert task_urls(node) == ["task?version=4"]


def test_the_task_carries_the_last_version_only_when_there_is_one(node):
    node.run("once")
    assert task_urls(node) == ["task"]
    for junk in ("", "abc\n", "-2\n"):
        node.write("last-version", junk)
        (node.base / "requests.jsonl").unlink()
        node.run("once")
        assert task_urls(node) == ["task"], junk


def test_a_task_older_than_the_last_line_is_ignored(node):
    # The control plane served a state from before this node's last PUT: rotating again would
    # throw away the SNI it just installed.
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.write("last-version", "3\n")
    node.respond("GET", "task", [200, '{"rotate":true,"version":2}'])
    result = node.run("once")
    assert result.status == 0
    assert not any(call.startswith(("validate", "docker")) for call in result.calls)
    assert node.backup_domains() == ["a.example.com", "b.example.com"] and node.sent("PUT", "line") == []
    node.respond("GET", "task", [200, '{"rotate":true,"version":3}'])
    node.run("once")
    assert node.sent("PUT", "line") == [{"sni": "a.example.com", "dest_ip": "203.0.113.10"}]


def test_a_first_401_does_not_stop_the_service(node):
    node.respond("GET", "task", [401, "unauthorized"])
    before = int(time.time())
    result = node.run("once")
    assert result.status == 0 and not node.exists("stopped")
    assert before <= int(node.read("unauthorized-since")) <= int(time.time())
    # 401s within two minutes of the first one do not stop it either
    node.write("unauthorized-since", f"{int(time.time()) - 100}\n")
    node.run("once")
    assert not node.exists("stopped")
    # any other answer ends the spell
    node.respond("GET", "task", NO_ROTATE)
    node.run("once")
    assert not node.exists("unauthorized-since") and not node.exists("stopped")


def test_a_401_on_the_pending_line_keeps_the_line(node):
    node.write("line-pending.json", LINE_A + "\n")
    node.respond("PUT", "line", [401, "unauthorized"])
    node.respond("POST", "event", [204, ""])
    assert node.run("once").status == 0
    assert node.read("line-pending.json").strip() == LINE_A and not node.exists("stopped")
    assert node.sent("POST", "event") == []


def test_a_line_carries_the_last_version(node):
    node.write("line-pending.json", LINE_A + "\n")
    node.write("last-version", "3\n")
    node.respond("PUT", "line", [200, '{"version":4}'])
    node.run("once")
    (put,) = [r for r in node.requests() if r["key"].endswith("/line")]
    assert put["url"].endswith(f"/node/{ALIAS}/line?version=3")
