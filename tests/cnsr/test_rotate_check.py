"""cnsr-rotate.sh check: one look at the control plane for cnsr up, changing nothing."""

from __future__ import annotations

import signal
import sys
import time

import pytest
from rotate_harness import ALIAS, TOKEN, node  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

LINE = '{"sni":"a.example.com","dest_ip":"203.0.113.10"}'


@pytest.mark.parametrize("status", [200, 401, 403, 404, 500])
def test_check_prints_the_status_of_one_plain_task_request(node, status):
    node.write("last-version", "3\n")
    node.respond("GET", "task", [status, '{"rotate":true,"version":3}'])
    result = node.run("check")
    assert result.status == 0
    assert result.stdout == f"{status}\n"
    (request,) = node.requests()
    # Without ?version the control plane only reads: the check never repairs its state.
    assert request["url"].endswith(f"/node/{ALIAS}/task") and TOKEN in request["config"]


def test_check_prints_000_when_the_control_plane_is_not_reached(node):
    node.respond("GET", "task", [0, ""])
    result = node.run("check")
    assert result.status == 0 and result.stdout == "000\n"


def test_check_leaves_the_service_state_alone(node):
    node.write("line-pending.json", LINE + "\n")
    node.respond("GET", "task", [401, "unauthorized"])
    result = node.run("check")
    assert result.stdout == "401\n"
    # no 401 spell started, no pending line written, no rotation, no response file left behind
    assert not node.exists("unauthorized-since")
    assert node.sent("PUT", "line") == [] and node.read("line-pending.json") == LINE + "\n"
    assert not any(call.startswith(("validate", "docker")) for call in result.calls)
    assert sorted(p.name for p in node.root.iterdir() if p.name.startswith(".http")) == []


def test_an_unknown_mode_is_refused(node):
    result = node.run("status")
    assert result.status == 1 and "check" in result.stderr and node.requests() == []


def test_a_term_during_a_check_leaves_the_services_switch_alone(node):
    # The running service may be halfway through a switch: undoing it is the service's job.
    node.write("switching", LINE + "\n")
    node.write("xray/conf/config.prev.json", node.read("xray/conf/config.json"), 0o644)
    (node.base / "curl-blocks").touch()
    process = node.start("check")
    deadline = time.monotonic() + 20
    while not (node.base / "curl-started").exists():
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=20) == 143
    assert node.read("switching") == LINE + "\n" and node.exists("xray/conf/config.prev.json")
    assert not any(call.startswith("docker") for call in node.calls())
