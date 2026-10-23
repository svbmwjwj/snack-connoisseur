"""cnsr-rotate.sh when things go wrong mid-way: interrupted switches, unwritable lines, bad backups."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest
from rotate_harness import OLD_SNI, node, xray_config  # noqa: F401  (fixture)
from test_rotate_script import NO_ROTATE, ROTATE, SERVICE, A, B, rotating

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

LINE_A = '{"sni":"a.example.com","dest_ip":"203.0.113.10"}'
ENTRY_A = '{"domain":"a.example.com","ip":"203.0.113.10","latency_ms":40}'


def cut_short_switch(node, *, pending: str | None = None):
    """The state a switch to A leaves when it is killed after the new config went in."""
    node.write("xray/conf/config.prev.json", json.dumps(xray_config(), indent=2) + "\n", 0o644)
    node.write("xray/conf/config.json", json.dumps(xray_config(*A), indent=2) + "\n", 0o644)
    node.write("switching", f"{LINE_A}\n{ENTRY_A}\n")
    if pending is not None:
        node.write("line-pending.json", pending + "\n")


def test_a_switch_cut_short_is_undone_before_the_next_round(node):
    rotating(node, B)
    node.respond("GET", "task", NO_ROTATE)
    cut_short_switch(node)
    result = node.run("once")
    assert result.status == 0, result.stderr
    assert node.reality()["serverNames"] == [OLD_SNI] and node.mode("xray/conf/config.json") == 0o644
    assert node.jsonl("restarts.jsonl") == [[OLD_SNI]]
    assert node.backup_domains() == ["a.example.com", "b.example.com"]
    assert not node.exists("switching") and node.sent("PUT", "line") == []


def test_a_switch_whose_line_was_recorded_is_kept_and_written(node):
    rotating(node)
    node.respond("GET", "task", NO_ROTATE)
    cut_short_switch(node, pending=LINE_A)
    node.run("once")
    assert node.reality()["serverNames"] == ["a.example.com"] and node.jsonl("restarts.jsonl") == []
    assert node.sent("PUT", "line") == [json.loads(LINE_A)]
    assert not node.exists("switching") and not node.exists("line-pending.json")


def test_a_switch_that_cannot_be_undone_holds_back_the_round(node):
    rotating(node, B)
    cut_short_switch(node)
    node.fail("docker-restart.rc", 1)
    result = node.run("once")
    assert result.status == 0 and node.exists("switching")
    assert node.requests() == [] and node.backup_domains() == ["b.example.com"]


def test_a_term_during_the_self_check_rolls_back(node):
    rotating(node, A, B)
    node.valid("a.example.com")
    (node.base / "verify-blocks").touch()
    process = node.start()
    deadline = time.monotonic() + 20
    while not (node.base / "verify-started").exists():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=20) == 143  # cnsr-rotate.service: SuccessExitStatus=143
    assert node.reality()["serverNames"] == [OLD_SNI]
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI]]
    assert node.backup_domains() == ["a.example.com", "b.example.com"]
    assert not node.exists("switching") and not node.exists("line-pending.json")


def test_a_new_line_that_cannot_be_recorded_rolls_back_and_reports(node):
    rotating(node, A, B)
    node.valid("a.example.com")
    (node.root / "line-pending.json.tmp").mkdir()  # writing the pending line fails
    node.run("once")
    assert node.reality()["serverNames"] == [OLD_SNI]
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI]]
    assert [e["kind"] for e in node.sent("POST", "event")] == ["rotate_failed"]
    assert node.backup_domains() == ["a.example.com", "b.example.com"]
    assert not node.exists("switching") and node.sent("PUT", "line") == []


@pytest.mark.parametrize("status", [400, 404, 409])
def test_a_line_the_control_plane_refuses_is_dropped_and_reported(node, status):
    rotating(node)
    node.write("line-pending.json", LINE_A + "\n")
    node.respond("PUT", "line", [status, "refused"])
    node.run("once")
    assert not node.exists("line-pending.json")
    (event,) = node.sent("POST", "event")
    assert event["kind"] == "rotate_failed" and str(status) in event["detail"]
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["line", "event"]


@pytest.mark.parametrize("answer", [[500, "error"], [503, "busy"], [0, ""], [429, "slow down"]])
def test_a_line_the_control_plane_could_not_take_waits_before_it_is_tried_again(node, answer):
    rotating(node)
    node.write("line-pending.json", LINE_A + "\n")
    node.respond("PUT", "line", answer)
    node.run("once")
    node.run("once")
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["line"]
    assert node.exists("line-pending.json") and node.sent("POST", "event") == []
    node.write("line-retry", f"{int(time.time()) - 301}\n")
    node.run("once")
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["line", "line"]


def test_entries_that_are_not_objects_are_dropped(node):
    rotating(node)
    entries = ["junk", 5, None, [1], {"domain": 7, "ip": "203.0.113.9"}, {"domain": A[0], "ip": A[1], "latency_ms": 40}]
    node.write("backups.json", json.dumps(entries))
    node.valid("a.example.com")
    result = node.run("once")
    assert result.status == 0, result.stderr
    assert node.reality()["serverNames"] == ["a.example.com"] and node.backup_domains() == []
    assert [c for c in result.calls if c.startswith("validate")] == ["validate-candidates.sh a.example.com 203.0.113.10"]


def test_a_rotation_request_after_a_cut_short_switch_uses_the_restored_config(node):
    rotating(node, B)
    node.respond("GET", "task", ROTATE)
    node.valid("b.example.com")
    cut_short_switch(node)
    node.run("once")
    # a was put back and tried first (it does not validate now), then b went in
    prev = json.loads(node.read("xray/conf/config.prev.json"))
    assert prev["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"] == [OLD_SNI]
    assert node.reality()["serverNames"] == ["b.example.com"]
    assert node.sent("PUT", "line") == [{"sni": "b.example.com", "dest_ip": "203.0.113.11"}]


def test_a_term_while_polling_exits_143(node):
    node.respond("GET", "task", NO_ROTATE)
    process = node.start()
    deadline = time.monotonic() + 20
    while not node.requests():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    # systemd sends TERM to the whole unit, the sleep between rounds included
    process.send_signal(signal.SIGTERM)
    subprocess.run(["pkill", "-TERM", "-P", str(process.pid)], check=False)
    assert process.wait(timeout=20) == 143
    assert not node.exists("stopped")


def test_the_stop_before_an_add_leaves_a_finished_switch_alone(node):
    # cnsr add stops the service first; its TERM handler only undoes an unfinished switch, so an
    # older config.prev.json and a line not yet written stay as they are.
    node.backups([B])
    node.write("xray/conf/config.prev.json", json.dumps(xray_config(), indent=2) + "\n", 0o644)
    node.write("xray/conf/config.json", json.dumps(xray_config(*A), indent=2) + "\n", 0o644)
    node.write("line-pending.json", LINE_A + "\n")
    node.respond("PUT", "line", [0, ""])
    before = {name: node.read(name) for name in ("xray/conf/config.json", "xray/conf/config.prev.json", "backups.json", "line-pending.json")}
    process = node.start()
    deadline = time.monotonic() + 20
    while not node.requests():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    subprocess.run(["pkill", "-TERM", "-P", str(process.pid)], check=False)
    assert process.wait(timeout=20) == 143
    assert {name: node.read(name) for name in before} == before
    assert node.jsonl("restarts.jsonl") == []


def test_the_unit_counts_only_the_term_exit_as_success():
    lines = SERVICE.read_text().splitlines()
    assert [line for line in lines if line.startswith("SuccessExitStatus=")] == ["SuccessExitStatus=143"]
    assert "Restart=on-failure" in lines


def test_the_unit_does_not_start_a_service_its_control_plane_refused():
    # stop() leaves /opt/cnsr/stopped; a reboot must not start polling with the refused token.
    assert "ConditionPathExists=!/opt/cnsr/stopped" in SERVICE.read_text().splitlines()
