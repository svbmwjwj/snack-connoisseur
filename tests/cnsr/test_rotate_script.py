"""Tests for cnsr-rotate.sh, the node's SNI rotation service, run against a fake node (see rotate_harness)."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest
from rotate_harness import ALIAS, CONTROL, OLD_SNI, PUBLIC_KEY, SCRIPT, SHORT_ID, TOKEN, UUID, node  # noqa: F401

from cnsr.server.xray import XRAY_IMAGE

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

ROTATE = [200, '{"rotate":true}']
NO_ROTATE = [200, '{"rotate":false}']
A, B, C = ("a.example.com", "203.0.113.10"), ("b.example.com", "203.0.113.11"), ("c.example.com", "203.0.113.12")
XRAY_TEST = (
    f"docker run --rm -v /opt/cnsr/xray/conf:/usr/local/etc/xray:ro {XRAY_IMAGE} "
    "run -test -c /usr/local/etc/xray/config.new.json"
)
RESTART = "docker compose -f /opt/cnsr/xray/compose.yml restart"
SERVICE = Path(SCRIPT).with_name("cnsr-rotate.service")


ONLINE_CHECK = "curl -s -o /dev/null --max-time 8 -w %{http_code} https://www.gstatic.com/generate_204"


def rotating(node, *backups):
    node.backups(list(backups))
    node.respond("GET", "task", ROTATE, NO_ROTATE)
    node.respond("PUT", "line", [200, '{"version":2}'])
    node.respond("POST", "event", [204, ""])
    return node


def test_no_request_changes_nothing(node):
    node.backups([A, B])
    before = (node.read("xray/conf/config.json"), node.read("backups.json"))
    result = node.run("once")
    assert result.status == 0
    assert [r["key"] for r in node.requests()] == [f"GET /node/{ALIAS}/task"]
    assert (node.read("xray/conf/config.json"), node.read("backups.json")) == before
    assert not any(call.startswith(("docker", "validate", "verify")) for call in result.calls)


def test_a_request_rotates_to_the_first_backup_that_validates(node):
    rotating(node, A, B, C)
    node.valid("b.example.com", "c.example.com")
    original = node.config()
    result = node.run("once")
    assert result.status == 0, result.stderr
    reality = node.reality()
    assert reality["dest"] == "203.0.113.11:443" and reality["serverNames"] == ["b.example.com"]
    # only dest and serverNames change
    original["inbounds"][0]["streamSettings"]["realitySettings"].update(dest=reality["dest"], serverNames=reality["serverNames"])
    assert node.config() == original
    assert node.mode("xray/conf/config.json") == 0o644  # Xray reads it as a non-root user
    assert node.backup_domains() == ["c.example.com"]
    assert node.sent("PUT", "line") == [{"sni": "b.example.com", "dest_ip": "203.0.113.11"}]
    assert node.sent("POST", "event") == []
    assert node.exists("xray/conf/config.prev.json") and not node.exists("xray/conf/config.new.json")
    prev = json.loads(node.read("xray/conf/config.prev.json"))
    assert prev["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"] == [OLD_SNI]
    assert [t["file"] for t in node.jsonl("tested.jsonl")] == ["/usr/local/etc/xray/config.new.json"]
    assert node.jsonl("restarts.jsonl") == [["b.example.com"]]
    assert result.calls[1:] == [
        "validate-candidates.sh a.example.com 203.0.113.10",
        ONLINE_CHECK,  # a.example.com failed: the node's own way out is checked before it is dropped
        "validate-candidates.sh b.example.com 203.0.113.11",
        result.calls[4],
        XRAY_TEST,
        RESTART,
        result.calls[7],
        result.calls[8],
    ]
    assert result.calls[4].startswith("china-check.sh b.example.com 203.0.113.11 ")  # and the seconds left
    assert result.calls[7].startswith("verify-reality.sh /opt/cnsr/") and result.calls[8].startswith("curl ")
    assert not node.exists("line-pending.json")


def test_the_self_check_gets_the_identity_and_the_new_sni_in_a_private_file(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.run("once")
    values = (node.base / "verify-values").read_text()
    assert values == f"UUID={UUID}\nPUBLIC_KEY={PUBLIC_KEY}\nSHORT_ID={SHORT_ID}\nSNI=a.example.com\n"
    assert (node.base / "verify-modes").read_text() == "-rw-------\n"


def test_a_backup_that_fails_the_self_check_is_rolled_back(node):
    rotating(node, A, B)
    node.valid("a.example.com", "b.example.com")
    node.self_check_fails("a.example.com")
    result = node.run("once")
    assert result.status == 0, result.stderr
    # a was tried and rolled back (restart with the old SNI), then b went in
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI], ["b.example.com"]]
    assert node.reality()["serverNames"] == ["b.example.com"] and node.reality()["dest"] == "203.0.113.11:443"
    assert node.backup_domains() == []
    assert node.sent("PUT", "line") == [{"sni": "b.example.com", "dest_ip": "203.0.113.11"}]


def test_the_last_backup_failing_the_self_check_leaves_the_old_config_and_reports_exhausted(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.self_check_fails("a.example.com")
    before = node.config()
    node.run("once")
    assert node.config() == before
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI]]
    assert [e["kind"] for e in node.sent("POST", "event")] == ["exhausted"]


def test_exhausted_backups_are_reported_and_the_old_config_stays(node):
    rotating(node, A, B)
    before = node.read("xray/conf/config.json")
    result = node.run("once")
    assert result.status == 0
    assert node.read("xray/conf/config.json") == before
    assert node.backup_domains() == []
    (event,) = node.sent("POST", "event")
    assert event["kind"] == "exhausted" and isinstance(event["detail"], str) and event["detail"]
    assert node.sent("PUT", "line") == []
    assert not any(call.startswith("docker") for call in result.calls)


def test_an_empty_backup_list_is_reported_exhausted(node):
    rotating(node)
    node.run("once")
    assert [e["kind"] for e in node.sent("POST", "event")] == ["exhausted"]


def test_a_backup_that_is_not_a_domain_and_an_address_is_dropped_without_validating(node):
    rotating(node, ("bad domain", "203.0.113.10"), ("x.example.com", "not-an-ip"), A)
    node.valid("a.example.com")
    result = node.run("once")
    assert [c for c in result.calls if c.startswith("validate")] == ["validate-candidates.sh a.example.com 203.0.113.10"]
    assert node.reality()["serverNames"] == ["a.example.com"]


def test_a_line_that_could_not_be_written_is_written_on_the_next_tick(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.respond("PUT", "line", [500, "error"], [200, '{"version":2}'])
    node.respond("GET", "task", ROTATE, NO_ROTATE)  # PUT line clears the request
    assert node.run("once").status == 0
    assert node.reality()["serverNames"] == ["a.example.com"]
    assert node.read("line-pending.json").strip() == '{"sni":"a.example.com","dest_ip":"203.0.113.10"}'
    node.write("line-retry", f"{int(time.time()) - 301}\n")  # the retry wait is over
    result = node.run("once")
    assert result.status == 0
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["task", "line", "line", "task"]
    assert node.sent("PUT", "line")[-1] == {"sni": "a.example.com", "dest_ip": "203.0.113.10"}
    assert not node.exists("line-pending.json")
    assert not any(call.startswith(("docker", "validate")) for call in result.calls)


def test_a_pending_line_that_still_cannot_be_written_holds_back_the_task(node):
    rotating(node, B)
    node.write("line-pending.json", '{"sni":"a.example.com","dest_ip":"203.0.113.10"}\n')
    node.respond("PUT", "line", [0, ""])
    result = node.run("once")
    assert result.status == 0
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["line"]
    assert node.exists("line-pending.json") and node.backup_domains() == ["b.example.com"]


def test_401s_for_more_than_two_minutes_stop_the_service(node):
    node.respond("GET", "task", [401, "unauthorized"])
    node.write("unauthorized-since", f"{int(time.time()) - 121}\n")
    result = node.run("once")
    assert result.status == 0 and node.exists("stopped") and not node.exists("unauthorized-since")
    # without "once" it polls until the 401, then exits 0 instead of polling on
    node.root.joinpath("stopped").unlink()
    node.write("unauthorized-since", f"{int(time.time()) - 121}\n")
    result = node.run(timeout=20)
    assert result.status == 0 and node.exists("stopped")


def test_401s_on_the_pending_line_stop_the_service(node):
    node.write("line-pending.json", '{"sni":"a.example.com","dest_ip":"203.0.113.10"}\n')
    node.write("unauthorized-since", f"{int(time.time()) - 121}\n")
    node.respond("PUT", "line", [401, "unauthorized"])
    assert node.run("once").status == 0 and node.exists("stopped")
    assert [r["key"].split("/")[-1] for r in node.requests()] == ["line"]


def test_every_request_has_the_user_agent_a_timeout_and_the_token_header(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.run("once")
    requests = node.requests()
    assert [r["key"] for r in requests] == [f"GET /node/{ALIAS}/task", f"PUT /node/{ALIAS}/line"]
    for request in requests:
        assert request["url"].startswith(f"{CONTROL}/node/{ALIAS}/")
        args = request["args"]
        assert args[args.index("-A") + 1] == "cnsr-node/1"
        assert args[args.index("--max-time") + 1] == "10"
        assert args[args.index("-K") + 1] == "-"
        assert request["config"] == f'header = "Authorization: Bearer {TOKEN}"\n'


def test_the_node_token_never_appears_on_a_command_line(node):
    rotating(node, A)
    node.valid("a.example.com")
    result = node.run("once")
    assert len(node.requests()) == 2
    assert result.calls and not any(TOKEN in call for call in result.calls)
    assert TOKEN not in result.stdout + result.stderr and UUID not in result.stdout + result.stderr


def test_missing_docker_reports_rotate_failed(node):
    rotating(node, A)
    node.valid("a.example.com")
    (node.bin / "docker").unlink()
    before = (node.read("xray/conf/config.json"), node.read("backups.json"))
    result = node.run("once")
    assert result.status == 0
    (event,) = node.sent("POST", "event")
    assert event["kind"] == "rotate_failed" and "docker" in event["detail"]
    assert (node.read("xray/conf/config.json"), node.read("backups.json")) == before
    assert node.sent("PUT", "line") == []


def test_a_rotate_failure_is_reported_once_an_hour_not_every_tick(node):
    rotating(node, A)
    (node.bin / "docker").unlink()
    node.respond("GET", "task", ROTATE)
    node.run("once")
    node.run("once")
    assert len(node.sent("POST", "event")) == 1
    node.write("rotate-failed", f"{int(time.time()) - 3601}\n")
    node.run("once")
    assert len(node.sent("POST", "event")) == 2


@pytest.mark.parametrize("failing", ["validate.rc", "docker-test.rc"])
def test_a_measurement_or_xray_test_error_reports_rotate_failed_and_keeps_the_backup(node, failing):
    rotating(node, A, B)
    node.valid("a.example.com")
    node.fail(failing, 1)
    before = (node.read("xray/conf/config.json"), node.read("backups.json"))
    node.run("once")
    assert [e["kind"] for e in node.sent("POST", "event")] == ["rotate_failed"]
    assert (node.read("xray/conf/config.json"), node.read("backups.json")) == before
    assert node.jsonl("restarts.jsonl") == [] and not node.exists("xray/conf/config.new.json")


def test_a_failed_restart_rolls_back_and_reports_rotate_failed(node):
    rotating(node, A)
    node.valid("a.example.com")
    node.fail("docker-restart.rc", 1)
    before = node.config()
    node.run("once")
    assert node.config() == before
    assert node.jsonl("restarts.jsonl") == [["a.example.com"], [OLD_SNI]]
    assert [e["kind"] for e in node.sent("POST", "event")] == ["rotate_failed"]


def test_a_control_plane_that_cannot_be_reached_changes_nothing(node):
    node.backups([A])
    node.respond("GET", "task", [0, ""])
    result = node.run("once")
    assert result.status == 0 and node.backup_domains() == ["a.example.com"] and not node.exists("stopped")


def test_the_script_uses_the_image_of_the_server():
    assert re.findall(r"^XRAY_IMAGE=(\S+)$", SCRIPT.read_text(), re.MULTILINE) == [XRAY_IMAGE]


def test_the_service_unit():
    lines = SERVICE.read_text().splitlines()
    for line in ("Type=simple", "ExecStart=/opt/cnsr/bin/cnsr-rotate.sh", "Restart=on-failure", "RestartSec=30",
                 "WantedBy=multi-user.target"):
        assert line in lines
