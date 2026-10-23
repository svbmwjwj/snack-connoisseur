"""Lightsail: finding a static IP the user already has by its address, and detaching it."""

from __future__ import annotations

import pytest

from cnsr.aws.errors import LightsailError
from cnsr.aws.static_ips import DETACH_POLL_SECONDS, DETACH_TIMEOUT_SECONDS
from cnsr.errors import CnsrError

pytestmark = pytest.mark.usefixtures("no_aws_environment")


def test_find_static_ip_names_the_ip_and_the_instance_it_is_on(aws):
    aws.ok("get_static_ips", {"staticIps": [
        {"name": "other-ip", "ipAddress": "192.0.2.1", "isAttached": False},
        {"name": "Debian-1-ip", "ipAddress": "192.0.2.10", "isAttached": True, "attachedTo": "Debian-1"},
    ]})  # fmt: skip
    assert aws.ls.find_static_ip("192.0.2.10") == ("Debian-1-ip", "Debian-1")


def test_find_static_ip_reports_a_free_one_as_attached_to_nothing(aws):
    aws.ok("get_static_ips", {"staticIps": [{"name": "spare", "ipAddress": "192.0.2.10", "isAttached": False}]})
    assert aws.ls.find_static_ip("192.0.2.10") == ("spare", None)


def test_find_static_ip_reads_every_page(aws):
    aws.ok("get_static_ips", {"staticIps": [{"name": "a", "ipAddress": "192.0.2.1"}], "nextPageToken": "p2"})
    aws.ok("get_static_ips", {"staticIps": [{"name": "b", "ipAddress": "192.0.2.10"}]}, pageToken="p2")
    assert aws.ls.find_static_ip("192.0.2.10") == ("b", None)


def test_find_static_ip_is_none_for_an_address_the_region_does_not_have(aws):
    aws.ok("get_static_ips", {"staticIps": [{"name": "a", "ipAddress": "192.0.2.1"}]})
    assert aws.ls.find_static_ip("192.0.2.10") is None


def test_find_static_ip_refuses_one_attached_to_an_instance_aws_does_not_name(aws):
    aws.ok("get_static_ips", {"staticIps": [{"name": "x-ip", "ipAddress": "192.0.2.10", "isAttached": True}]})
    with pytest.raises(CnsrError) as info:
        aws.ls.find_static_ip("192.0.2.10")
    assert "x-ip" in str(info.value) and "没有给出名字" in str(info.value)


def ip_state(aws, name: str, attached: bool) -> None:
    aws.ok("get_static_ip", {"staticIp": {"name": name, "isAttached": attached}}, staticIpName=name)


def test_detach_static_ip_detaches_and_waits_until_lightsail_shows_it_free(aws):
    aws.ok("detach_static_ip", staticIpName="Debian-1-ip")
    ip_state(aws, "Debian-1-ip", True)
    ip_state(aws, "Debian-1-ip", False)
    aws.ls.detach_static_ip("Debian-1-ip")
    assert len(aws.sleeps) == 1


def test_detach_static_ip_gives_up_waiting_with_an_error(aws):
    aws.ok("detach_static_ip", staticIpName="Debian-1-ip")
    waits = int(DETACH_TIMEOUT_SECONDS / DETACH_POLL_SECONDS)
    for _ in range(waits + 1):
        ip_state(aws, "Debian-1-ip", True)
    with pytest.raises(CnsrError) as info:
        aws.ls.detach_static_ip("Debian-1-ip")
    assert "仍显示" in str(info.value)


def test_detach_static_ip_accepts_one_that_is_not_attached(aws):
    aws.fail("detach_static_ip", "InvalidInputException", "Static IP is not attached", staticIpName="spare")
    ip_state(aws, "spare", False)
    aws.ls.detach_static_ip("spare")


def test_a_failed_detach_is_an_error(aws):
    aws.fail("detach_static_ip", "AccessDeniedException", staticIpName="Debian-1-ip")
    with pytest.raises(LightsailError) as info:
        aws.ls.detach_static_ip("Debian-1-ip")
    assert "解绑静态 IP" in str(info.value)


def test_the_detach_wait_counts_the_time_its_calls_take(aws):
    aws.call_seconds = DETACH_POLL_SECONDS
    aws.ok("detach_static_ip", staticIpName="Debian-1-ip")
    for _ in range(int(DETACH_TIMEOUT_SECONDS / DETACH_POLL_SECONDS / 2) + 1):
        ip_state(aws, "Debian-1-ip", True)
    with pytest.raises(CnsrError):
        aws.ls.detach_static_ip("Debian-1-ip")
    assert aws.now <= DETACH_TIMEOUT_SECONDS + 2 * DETACH_POLL_SECONDS
