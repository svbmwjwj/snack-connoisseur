"""Hetzner Primary IPs against a scripted API: create, own, swap and delete."""

from __future__ import annotations

import httpx
import pytest

from cnsr.errors import CnsrError
from hetzner_harness import done, fail, primary_ip, server


def ips(hz_api, *listed: dict) -> None:
    hz_api.on("GET", "/primary_ips", {"primary_ips": list(listed)})


def test_creates_an_unassigned_labelled_ipv4_in_the_location(hz_api):
    ips(hz_api)
    hz_api.on("POST", "/primary_ips", {"primary_ip": primary_ip("de-1-ip"), "action": None})
    assert hz_api.hz.ensure_primary_ip("de-1-ip", "de-1") == "203.0.113.7"
    assert hz_api.sent("POST", "/primary_ips") == [{
        "name": "de-1-ip", "type": "ipv4", "location": "fsn1", "auto_delete": True, "labels": {"cnsr": "de-1"},
    }]
    assert hz_api.calls[0][2] == {"name": "de-1-ip"}


def test_a_create_action_is_waited_for(hz_api):
    ips(hz_api)
    hz_api.on("POST", "/primary_ips",
              {"primary_ip": primary_ip("de-1-ip"), "action": {**done("create_primary_ip", 6), "status": "running"}})
    hz_api.on("GET", "/actions/6", {"action": done("create_primary_ip", 6)})
    hz_api.hz.ensure_primary_ip("de-1-ip", "de-1")
    assert "GET /actions/6" in hz_api.paths()


def test_accepts_its_own_ip(hz_api):
    ips(hz_api, primary_ip("de-1-ip", assignee=10))
    assert hz_api.hz.ensure_primary_ip("de-1-ip", "de-1") == "203.0.113.7"
    assert hz_api.sent("POST", "/primary_ips") == []


@pytest.mark.parametrize("alias", ["", "other"])
def test_refuses_a_same_named_ip_without_the_label(hz_api, alias):
    ips(hz_api, primary_ip("de-1-ip", alias=alias))
    with pytest.raises(CnsrError, match="不是 cnsr 创建的") as caught:
        hz_api.hz.ensure_primary_ip("de-1-ip", "de-1")
    assert "cnsr=de-1" in str(caught.value)
    assert hz_api.sent("POST", "/primary_ips") == []


def test_refuses_its_own_ip_in_another_location(hz_api):
    ips(hz_api, primary_ip("de-1-ip", location={"name": "hel1"}))
    with pytest.raises(CnsrError, match="hel1"):
        hz_api.hz.ensure_primary_ip("de-1-ip", "de-1")


# --- swap


def test_unassign_waits_for_the_action(hz_api):
    ips(hz_api, primary_ip("de-1-ip", assignee=10))
    hz_api.on("POST", "/primary_ips/20/actions/unassign", {"action": {**done("unassign_primary_ip", 8), "status": "running"}})
    hz_api.on("GET", "/actions/8", {"action": done("unassign_primary_ip", 8)})
    hz_api.hz.unassign_primary_ip("de-1-ip")
    assert hz_api.paths() == ["GET /primary_ips", "POST /primary_ips/20/actions/unassign", "GET /actions/8"]


def test_unassign_of_a_free_ip_does_nothing(hz_api):
    ips(hz_api, primary_ip("de-1-ip"))
    hz_api.hz.unassign_primary_ip("de-1-ip")
    assert hz_api.paths() == ["GET /primary_ips"]


def test_unassign_from_a_running_server_reports_it(hz_api):
    ips(hz_api, primary_ip("de-1-ip", assignee=10))
    hz_api.on("POST", "/primary_ips/20/actions/unassign", fail(422, "server_not_stopped"))
    with pytest.raises(CnsrError, match="先关机"):
        hz_api.hz.unassign_primary_ip("de-1-ip")


def test_unassign_of_a_missing_ip_is_an_error(hz_api):
    ips(hz_api)
    with pytest.raises(CnsrError, match="找不到 Primary IP de-1-ip"):
        hz_api.hz.unassign_primary_ip("de-1-ip")


def test_create_assigned_goes_straight_to_the_server_without_a_location(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1", status="off")]})
    hz_api.on("POST", "/primary_ips",
              {"primary_ip": primary_ip("de-1-ip-new", ip="203.0.113.99", assignee=10), "action": done("assign")})
    assert hz_api.hz.create_assigned_primary_ip("de-1-ip-new", "de-1", "de-1") == "203.0.113.99"
    assert hz_api.sent("POST", "/primary_ips") == [{
        "name": "de-1-ip-new", "type": "ipv4", "assignee_type": "server", "assignee_id": 10,
        "auto_delete": True, "labels": {"cnsr": "de-1"},
    }]


def test_rename(hz_api):
    ips(hz_api, primary_ip("de-1-ip-new"))
    hz_api.on("PUT", "/primary_ips/20", {"primary_ip": primary_ip("de-1-ip")})
    hz_api.hz.rename_primary_ip("de-1-ip-new", "de-1-ip")
    assert hz_api.sent("PUT", "/primary_ips/20") == [{"name": "de-1-ip"}]


# --- delete


def test_delete_a_free_own_ip(hz_api):
    ips(hz_api, primary_ip("de-1-ip"))
    hz_api.on("DELETE", "/primary_ips/20", lambda _: httpx.Response(204))
    assert hz_api.hz.delete_primary_ip("de-1-ip", "de-1") == []
    assert "DELETE /primary_ips/20" in hz_api.paths()


def test_delete_a_missing_ip_is_done(hz_api):
    ips(hz_api)
    assert hz_api.hz.delete_primary_ip("de-1-ip", "de-1") == []


def test_delete_gone_meanwhile_is_done(hz_api):
    ips(hz_api, primary_ip("de-1-ip"))
    hz_api.on("DELETE", "/primary_ips/20", fail(404, "not_found"))
    assert hz_api.hz.delete_primary_ip("de-1-ip", "de-1") == []


def test_delete_leaves_a_strangers_ip(hz_api):
    ips(hz_api, primary_ip("de-1-ip", alias=""))
    (error,) = hz_api.hz.delete_primary_ip("de-1-ip", "de-1")
    assert "不是 cnsr 创建的" in error
    assert "DELETE /primary_ips/20" not in hz_api.paths()


def test_delete_leaves_an_assigned_ip(hz_api):
    ips(hz_api, primary_ip("de-1-ip", assignee=10))
    (error,) = hz_api.hz.delete_primary_ip("de-1-ip", "de-1")
    assert "还分配在一台服务器上" in error
    assert "DELETE /primary_ips/20" not in hz_api.paths()


def test_delete_returns_the_chinese_error(hz_api):
    ips(hz_api, primary_ip("de-1-ip"))
    hz_api.on("DELETE", "/primary_ips/20", fail(423, "locked"))
    (error,) = hz_api.hz.delete_primary_ip("de-1-ip", "de-1")
    assert "动作在进行" in error


def test_a_failed_lookup_is_returned_as_an_error(hz_api):
    hz_api.on("GET", "/primary_ips", fail(503, "unavailable"))
    (error,) = hz_api.hz.delete_primary_ip("de-1-ip", "de-1")
    assert "HTTP 503" in error
