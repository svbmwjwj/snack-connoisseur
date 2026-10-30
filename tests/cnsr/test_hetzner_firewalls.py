"""Hetzner cloud firewalls against a scripted API: create, apply, set rules, delete by label."""

from __future__ import annotations

import httpx
import pytest

from cnsr.errors import CnsrError
from cnsr.server.xray import PORTS
from hetzner_harness import done, fail, firewall, server

OPEN = ["0.0.0.0/0", "::/0"]


def firewalls(hz_api, *listed: dict) -> None:
    hz_api.on("GET", "/firewalls", {"firewalls": list(listed), "meta": None})


def running(command: str, action_id: int) -> dict:
    return {**done(command, action_id), "status": "running"}


def test_creates_a_labelled_firewall_without_rules(hz_api):
    firewalls(hz_api)
    hz_api.on("POST", "/firewalls", {"firewall": firewall("de-1-fw"), "actions": []})
    hz_api.hz.ensure_firewall("de-1-fw", "de-1")
    assert hz_api.sent("POST", "/firewalls") == [{"name": "de-1-fw", "labels": {"cnsr": "de-1"}, "rules": []}]
    assert hz_api.calls[0][2] == {"name": "de-1-fw"}


def test_accepts_its_own_firewall(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    hz_api.hz.ensure_firewall("de-1-fw", "de-1")
    assert hz_api.paths() == ["GET /firewalls"]


def test_refuses_a_same_named_firewall_without_the_label(hz_api):
    firewalls(hz_api, firewall("de-1-fw", alias=""))
    with pytest.raises(CnsrError, match="不是 cnsr 创建的"):
        hz_api.hz.ensure_firewall("de-1-fw", "de-1")
    assert hz_api.sent("POST", "/firewalls") == []


def test_attach_applies_to_the_server_and_waits(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.on("POST", "/firewalls/30/actions/apply_to_resources", {"actions": [running("apply_firewall", 11)]})
    hz_api.on("GET", "/actions/11", {"action": done("apply_firewall", 11)})
    hz_api.hz.attach_firewall("de-1-fw", "de-1")
    assert hz_api.sent("POST", "/firewalls/30/actions/apply_to_resources") == [
        {"apply_to": [{"type": "server", "server": {"id": 10}}]}]
    assert hz_api.paths()[-1] == "GET /actions/11"


def test_attach_when_already_applied_does_nothing(hz_api):
    firewalls(hz_api, firewall("de-1-fw", servers=(10,)))
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.hz.attach_firewall("de-1-fw", "de-1")
    assert hz_api.sent("POST", "/firewalls/30/actions/apply_to_resources") == []


def test_attach_racing_an_earlier_apply_counts_as_done(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.on("POST", "/firewalls/30/actions/apply_to_resources", fail(422, "firewall_already_applied"))
    hz_api.hz.attach_firewall("de-1-fw", "de-1")


def test_attach_a_missing_firewall_is_an_error(hz_api):
    firewalls(hz_api)
    with pytest.raises(CnsrError, match="找不到防火墙 de-1-fw"):
        hz_api.hz.attach_firewall("de-1-fw", "de-1")


def test_set_rules_opens_ssh_and_app_ports_inbound_to_all_and_waits(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    hz_api.on("POST", "/firewalls/30/actions/set_rules",
              {"actions": [running("set_firewall_rules", 12), running("apply_firewall", 13)]})
    hz_api.on("GET", "/actions/12", {"action": done("set_firewall_rules", 12)})
    hz_api.on("GET", "/actions/13", {"action": done("apply_firewall", 13)})
    hz_api.hz.set_rules("de-1-fw", (22, 50022), PORTS)
    assert hz_api.sent("POST", "/firewalls/30/actions/set_rules") == [{"rules": [
        {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": OPEN},
        {"direction": "in", "protocol": "tcp", "port": "50022", "source_ips": OPEN},
        {"direction": "in", "protocol": "tcp", "port": "443", "source_ips": OPEN},
        {"direction": "in", "protocol": "udp", "port": "443", "source_ips": OPEN},
    ]}]
    assert hz_api.paths()[-2:] == ["GET /actions/12", "GET /actions/13"]


def test_set_rules_drops_duplicates(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    hz_api.on("POST", "/firewalls/30/actions/set_rules", {"actions": []})
    hz_api.hz.set_rules("de-1-fw", [22, 22], ((22, "tcp"),))
    (body,) = hz_api.sent("POST", "/firewalls/30/actions/set_rules")
    assert [rule["port"] for rule in body["rules"]] == ["22"]


def test_set_rules_fails_when_its_action_fails(hz_api):
    firewalls(hz_api, firewall("de-1-fw"))
    failed = {**done("set_firewall_rules"), "status": "error", "error": {"code": "x", "message": "boom"}}
    hz_api.on("POST", "/firewalls/30/actions/set_rules", {"actions": [failed]})
    with pytest.raises(CnsrError, match="set_firewall_rules"):
        hz_api.hz.set_rules("de-1-fw", (22,), ())


def test_delete_finds_firewalls_by_label_and_deletes_them(hz_api):
    firewalls(hz_api, firewall("de-1-fw"), firewall("renamed", fw_id=31))
    hz_api.on("DELETE", "/firewalls/30", lambda _: httpx.Response(204))
    hz_api.on("DELETE", "/firewalls/31", lambda _: httpx.Response(204))
    assert hz_api.hz.delete_firewall("de-1") == []
    assert hz_api.calls[0][2]["label_selector"] == "cnsr=de-1"
    assert hz_api.paths()[1:] == ["DELETE /firewalls/30", "DELETE /firewalls/31"]


def test_delete_with_none_left_is_done(hz_api):
    firewalls(hz_api)
    assert hz_api.hz.delete_firewall("de-1") == []


def test_delete_reports_one_still_in_use_and_goes_on(hz_api):
    firewalls(hz_api, firewall("de-1-fw"), firewall("second", fw_id=31))
    hz_api.on("DELETE", "/firewalls/30", fail(422, "resource_in_use"))
    hz_api.on("DELETE", "/firewalls/31", fail(404, "not_found"))
    (error,) = hz_api.hz.delete_firewall("de-1")
    assert "de-1-fw" in error and "拒绝删除" in error


def test_delete_returns_other_errors(hz_api):
    hz_api.on("GET", "/firewalls", fail(401, "unauthorized"))
    (error,) = hz_api.hz.delete_firewall("de-1")
    assert "token 无效" in error
