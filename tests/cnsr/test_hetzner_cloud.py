"""Hetzner SSH keys, servers and power against a scripted API (no network, no real token)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cnsr.errors import CnsrError
from cnsr.hetzner import cloud
from cnsr.hetzner.cloud import Hetzner, KeyConflict
from cnsr.hetzner.sizes import Offer
from hetzner_harness import TB, done, fail, pricing, primary_ip, server, stype
from key_samples import ED, ED_MD5, RSA_MD5

OFFER = Offer("cpx02", "cpx02", 1, 1, 20, 20 * TB, Decimal("1"), Decimal("4"), Decimal("0.6"), "EUR", "regular_purpose")


def test_region_table():
    assert cloud.REGIONS == {
        "sg": "sin", "us-oregon": "hil", "us-virginia": "ash",
        "de-falkenstein": "fsn1", "de-nuremberg": "nbg1", "fi-helsinki": "hel1",
    }
    assert set(cloud.REGION_NAMES) == set(cloud.REGIONS)
    assert cloud.REGION_NAMES["de-falkenstein"] == "德国法尔肯施泰因"
    assert cloud.IMAGES == ("debian-13", "debian-12")


def test_an_unknown_region_lists_the_valid_ones(hz_api):
    with pytest.raises(CnsrError) as caught:
        Hetzner("jp-tokyo", hz_api.hz._client)
    assert "jp-tokyo" in str(caught.value)
    assert "sg、us-oregon、us-virginia、de-falkenstein、de-nuremberg、fi-helsinki" in str(caught.value)


def test_the_location_follows_the_region(hz_api):
    assert (hz_api.hz.region_code, hz_api.hz.location) == ("de-falkenstein", "fsn1")


# --- SSH keys


def keys(*pairs: tuple[str, str]) -> dict:
    return {"ssh_keys": [{"id": i, "name": n, "fingerprint": f} for i, (n, f) in enumerate(pairs)], "meta": None}


def test_reuses_a_key_with_the_same_fingerprint_under_any_name(hz_api):
    hz_api.on("GET", "/ssh_keys", keys(("other", RSA_MD5), ("old-key", ED_MD5)))
    assert hz_api.hz.ensure_key_pair("de-1", ED) == "old-key"


def test_a_same_name_match_wins(hz_api):
    hz_api.on("GET", "/ssh_keys", keys(("first", ED_MD5), ("de-1", ED_MD5.upper())))
    assert hz_api.hz.ensure_key_pair("de-1", ED) == "de-1"


def test_same_name_different_fingerprint_conflicts(hz_api):
    hz_api.on("GET", "/ssh_keys", keys(("de-1", RSA_MD5)))
    with pytest.raises(KeyConflict, match="de-1"):
        hz_api.hz.ensure_key_pair("de-1", ED)
    assert hz_api.sent("POST", "/ssh_keys") == []


def test_uploads_the_clean_key_when_none_matches(hz_api):
    hz_api.on("GET", "/ssh_keys", keys(("other", RSA_MD5)))
    hz_api.on("POST", "/ssh_keys", {"ssh_key": {"id": 9, "name": "de-1"}})
    assert hz_api.hz.ensure_key_pair("de-1", "\ufeff" + ED + "\r\n") == "de-1"
    assert hz_api.sent("POST", "/ssh_keys") == [{"name": "de-1", "public_key": ED}]


def test_keys_are_read_page_by_page(hz_api):
    hz_api.on("GET", "/ssh_keys",
              {**keys(("a", RSA_MD5)), "meta": {"pagination": {"next_page": 2}}},
              keys(("old-key", ED_MD5)))
    assert hz_api.hz.ensure_key_pair("de-1", ED) == "old-key"


def test_a_bad_public_key_fails_before_any_call(hz_api):
    with pytest.raises(CnsrError, match="公钥"):
        hz_api.hz.ensure_key_pair("de-1", "ssh-ed25519 AAAA")
    assert hz_api.calls == []


# --- images


def images(*listed: tuple[str, str]) -> dict:
    return {"images": [{"id": i, "name": n, "status": s} for i, (n, s) in enumerate(listed)], "meta": None}


def test_pick_image_prefers_debian_13_and_asks_for_x86_system_images(hz_api):
    hz_api.on("GET", "/images", images(("debian-12", "available"), ("debian-13", "available")))
    assert hz_api.hz.pick_image() == "debian-13"
    assert hz_api.calls[0][2] == {"type": "system", "architecture": "x86", "page": "1", "per_page": "50"}


def test_pick_image_falls_back_to_debian_12(hz_api):
    hz_api.on("GET", "/images", images(("debian-13", "unavailable"), ("debian-12", "available")))
    assert hz_api.hz.pick_image() == "debian-12"


def test_pick_image_without_debian_is_an_error(hz_api):
    hz_api.on("GET", "/images", images(("ubuntu-24.04", "available")))
    with pytest.raises(CnsrError, match="Debian"):
        hz_api.hz.pick_image()


# --- servers


def no_server(hz_api) -> None:
    hz_api.on("GET", "/servers", {"servers": []})


def ready_to_create(hz_api) -> None:
    no_server(hz_api)
    hz_api.on("GET", "/primary_ips", {"primary_ips": [primary_ip("de-1-ip", ip_id=20)]})
    hz_api.on("GET", "/images", images(("debian-13", "available")))


def test_creates_a_labelled_dual_stack_server_on_the_primary_ip(hz_api):
    ready_to_create(hz_api)
    hz_api.on("POST", "/servers", {"server": server("de-1"), "action": done("create_server")})
    assert hz_api.hz.ensure_instance("de-1", "my-key", OFFER, "de-1-ip") is True
    assert hz_api.sent("POST", "/servers") == [{
        "name": "de-1",
        "location": "fsn1",
        "server_type": "cpx02",
        "image": "debian-13",
        "ssh_keys": ["my-key"],
        "labels": {"cnsr": "de-1"},
        "public_net": {"enable_ipv4": True, "ipv4": 20, "enable_ipv6": True},
        "start_after_create": True,
    }]
    assert hz_api.calls[1][2] == {"name": "de-1-ip"}


def test_the_create_action_is_waited_for(hz_api):
    ready_to_create(hz_api)
    hz_api.on("POST", "/servers", {"server": server("de-1"), "action": {**done("create_server", 7), "status": "running"}})
    hz_api.on("GET", "/actions/7", {"action": done("create_server", 7)})
    hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    assert "GET /actions/7" in hz_api.paths()


def test_the_start_server_next_action_is_waited_for_too(hz_api):
    ready_to_create(hz_api)
    start = {**done("start_server", 8), "status": "running"}
    created = {"server": server("de-1", status="initializing"), "action": done("create_server", 7)}
    hz_api.on("POST", "/servers", {**created, "next_actions": [start]})
    hz_api.on("GET", "/actions/8", {"action": done("start_server", 8)})
    hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    assert "GET /actions/8" in hz_api.paths()


def test_a_failed_start_server_stops_the_create(hz_api):
    ready_to_create(hz_api)
    start = {**done("start_server", 8), "status": "error", "error": {"code": "server_error", "message": "boom"}}
    hz_api.on("POST", "/servers", {"server": server("de-1"), "action": done("create_server", 7), "next_actions": [start]})
    with pytest.raises(CnsrError, match="start_server"):
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")


def test_accepts_its_own_server(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    assert hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip") is False
    assert hz_api.paths() == ["GET /servers"]


def test_refuses_a_same_named_server_without_the_label(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1", alias="")]})
    with pytest.raises(CnsrError, match="不是 cnsr 创建的") as caught:
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    assert "cnsr=de-1" in str(caught.value)
    assert hz_api.sent("POST", "/servers") == []


def test_a_missing_primary_ip_stops_before_creating(hz_api):
    no_server(hz_api)
    hz_api.on("GET", "/primary_ips", {"primary_ips": []})
    with pytest.raises(CnsrError, match="de-1-ip"):
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    assert hz_api.sent("POST", "/servers") == []


def stock(hz_api, *types) -> None:
    hz_api.on("GET", "/server_types", {"server_types": list(types), "meta": None})
    hz_api.on("GET", "/pricing", {"pricing": pricing()})


@pytest.mark.parametrize("code", ["resource_unavailable", "placement_error"])
def test_out_of_stock_lists_the_other_types_of_the_plan_and_buys_none(hz_api, code):
    ready_to_create(hz_api)
    hz_api.on("POST", "/servers", fail(422, code))
    stock(hz_api, stype("cpx02", 1, 1, "4"), stype("cpx12", 1, 2, "6.0000"), stype("cpx22", 2, 4, "9"),
          stype("gone", 1, 1, "1", available=False), stype("cx23", 2, 4, "3", category="cost_optimized"))
    with pytest.raises(CnsrError) as caught:
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    text = str(caught.value)
    assert "cpx02" in text and "de-falkenstein（德国法尔肯施泰因）" in text
    assert "1c2g  cpx12  1 核 / 2 GB 内存" in text and "约 6.61 欧元/月" in text
    assert "2c4g  cpx22" in text
    assert "1c1g  cpx02" not in text and "gone" not in text and "cx23" not in text  # another plan
    assert "不会自动换型号" in text
    assert len(hz_api.sent("POST", "/servers")) == 1


def test_a_failed_create_action_for_stock_also_lists_alternatives(hz_api):
    ready_to_create(hz_api)
    failed = {**done("create_server", 7), "status": "error", "error": {"code": "placement_error", "message": "x"}}
    hz_api.on("POST", "/servers", {"server": server("de-1"), "action": failed})
    stock(hz_api, stype("cpx12", 1, 2, "6"))
    with pytest.raises(CnsrError, match="cpx12"):
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")


def test_out_of_stock_with_nothing_else_to_buy_says_so(hz_api):
    ready_to_create(hz_api)
    hz_api.on("POST", "/servers", fail(412, "resource_unavailable"))
    stock(hz_api, stype("cpx02", 1, 1, "4"))
    with pytest.raises(CnsrError, match="暂时没有别的能买的型号"):
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")


def test_other_create_errors_pass_through(hz_api):
    ready_to_create(hz_api)
    hz_api.on("POST", "/servers", fail(403, "forbidden"))
    with pytest.raises(CnsrError, match="只读 token"):
        hz_api.hz.ensure_instance("de-1", "k", OFFER, "de-1-ip")
    assert "GET /server_types" not in hz_api.paths()


@pytest.mark.parametrize("listed,expected", [([], None), ([server("de-1")], True), ([server("de-1", alias="x")], False)])
def test_instance_tagged(hz_api, listed, expected):
    hz_api.on("GET", "/servers", {"servers": listed})
    assert hz_api.hz.instance_tagged("de-1", "de-1") is expected


# --- states


def statuses(hz_api, *states: str) -> None:
    hz_api.on("GET", "/servers", *({"servers": [server("de-1", status=s)]} for s in states))


def test_wait_running_polls_until_running(hz_api):
    statuses(hz_api, "initializing", "starting", "running")
    hz_api.hz.wait_running("de-1")
    assert hz_api.sleeps == [2, 2]


def test_wait_running_fails_at_once_on_another_state(hz_api):
    statuses(hz_api, "off")
    with pytest.raises(CnsrError, match="状态是 off"):
        hz_api.hz.wait_running("de-1")


def test_wait_running_gives_up_at_the_deadline(hz_api):
    statuses(hz_api, "starting")
    with pytest.raises(CnsrError, match="等了 10 秒"):
        hz_api.hz.wait_running("de-1", timeout=10)
    assert sum(hz_api.sleeps) == 10


def test_wait_running_for_a_missing_server_is_an_error(hz_api):
    no_server(hz_api)
    with pytest.raises(CnsrError, match="找不到服务器 de-1"):
        hz_api.hz.wait_running("de-1")


def test_power_off_waits_for_the_action_and_the_off_state(hz_api):
    statuses(hz_api, "running", "stopping", "off")
    hz_api.on("POST", "/servers/10/actions/poweroff", {"action": {**done("stop_server", 3), "status": "running"}})
    hz_api.on("GET", "/actions/3", {"action": done("stop_server", 3)})
    hz_api.hz.power_off("de-1")
    assert hz_api.paths() == ["GET /servers", "POST /servers/10/actions/poweroff", "GET /actions/3",
                              "GET /servers", "GET /servers"]


def test_power_off_of_a_server_already_off_does_nothing(hz_api):
    statuses(hz_api, "off")
    hz_api.hz.power_off("de-1")
    assert hz_api.sent("POST", "/servers/10/actions/poweroff") == []


def test_power_on_waits_until_running(hz_api):
    statuses(hz_api, "off", "starting", "running")
    hz_api.on("POST", "/servers/10/actions/poweron", {"action": done("start_server")})
    hz_api.hz.power_on("de-1")
    assert hz_api.paths().count("POST /servers/10/actions/poweron") == 1


def test_a_failed_power_action_is_an_error(hz_api):
    statuses(hz_api, "running")
    failed = {**done("stop_server"), "status": "error", "error": {"code": "locked", "message": "x"}}
    hz_api.on("POST", "/servers/10/actions/poweroff", {"action": failed})
    with pytest.raises(CnsrError, match="stop_server"):
        hz_api.hz.power_off("de-1")


# --- addresses and traffic


def test_instance_ipv6_is_the_first_address_of_the_64(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    assert hz_api.hz.instance_ipv6("de-1") == "2001:db8:1::1"


def test_instance_ipv6_without_one_is_none(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1", public_net={"ipv4": None, "ipv6": None})]})
    assert hz_api.hz.instance_ipv6("de-1") is None


def test_traffic_is_outgoing_and_included_bytes(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1", outgoing_traffic=123, included_traffic=456)]})
    assert hz_api.hz.traffic("de-1") == (123, 456)


def test_traffic_counts_nothing_reported_as_zero(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1", outgoing_traffic=None, included_traffic=456)]})
    assert hz_api.hz.traffic("de-1") == (0, 456)


def test_traffic_of_a_missing_server_is_none(hz_api):
    no_server(hz_api)
    assert hz_api.hz.traffic("de-1") is None


# --- removal


def test_delete_instance_deletes_and_waits(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.on("DELETE", "/servers/10", {"action": {**done("delete_server", 4), "status": "running"}})
    hz_api.on("GET", "/actions/4", {"action": done("delete_server", 4)})
    assert hz_api.hz.delete_instance("de-1") == []
    assert hz_api.paths() == ["GET /servers", "DELETE /servers/10", "GET /actions/4"]


def test_delete_instance_of_a_missing_server_is_done(hz_api):
    no_server(hz_api)
    assert hz_api.hz.delete_instance("de-1") == []


def test_delete_instance_gone_meanwhile_is_done(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.on("DELETE", "/servers/10", fail(404, "not_found"))
    assert hz_api.hz.delete_instance("de-1") == []


def test_delete_instance_returns_the_chinese_error(hz_api):
    hz_api.on("GET", "/servers", {"servers": [server("de-1")]})
    hz_api.on("DELETE", "/servers/10", fail(423, "locked"))
    (error,) = hz_api.hz.delete_instance("de-1")
    assert "HTTP 423" in error and "动作在进行" in error
