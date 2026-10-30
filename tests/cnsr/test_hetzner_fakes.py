"""The fake Hetzner stays in step with the real one."""

from __future__ import annotations

import inspect

import pytest

from cnsr.errors import CnsrError
from cnsr.hetzner.cloud import Hetzner
from hetzner_fakes import HZ_IP, FakeHetzner


def public(cls) -> dict[str, list[str]]:
    """Each public method and the names of its parameters without a default."""
    return {
        name: [
            p.name for p in inspect.signature(method).parameters.values()
            if p.name != "self" and p.default is inspect.Parameter.empty
        ]
        for name, method in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_the_fake_has_every_public_method_with_the_same_parameters():
    fake = public(FakeHetzner)
    fake.pop("names")
    assert fake == public(Hetzner)


def test_first_deploy_then_swap_then_delete():
    hz = FakeHetzner()
    assert hz.ensure_primary_ip("de-1-ip", "de-1") == HZ_IP
    assert hz.ensure_instance("de-1", "k", hz.pick("1c1g"), "de-1-ip") is True
    with pytest.raises(CnsrError, match="先关机"):
        hz.unassign_primary_ip("de-1-ip")
    hz.power_off("de-1")
    hz.unassign_primary_ip("de-1-ip")
    new = hz.create_assigned_primary_ip("de-1-ip-new", "de-1", "de-1")
    assert new != HZ_IP
    assert hz.delete_primary_ip("de-1-ip", "de-1") == []
    hz.rename_primary_ip("de-1-ip-new", "de-1-ip")
    hz.power_on("de-1")
    assert hz.assigned["de-1-ip"] == "de-1"
    hz.ensure_firewall("de-1-fw", "de-1")
    hz.attach_firewall("de-1-fw", "de-1")
    assert hz.delete_firewall("de-1") != []  # still applied
    assert hz.delete_instance("de-1") == []
    assert hz.ips == {} and hz.delete_firewall("de-1") == [] and hz.firewalls == {}


def test_strangers_resources_are_refused():
    hz = FakeHetzner()
    hz.servers["de-1"], hz.ips["de-1-ip"], hz.firewalls["de-1-fw"] = False, False, False
    for call in (lambda: hz.ensure_primary_ip("de-1-ip", "de-1"),
                 lambda: hz.ensure_instance("de-1", "k", hz.pick("1c1g"), "de-1-ip"),
                 lambda: hz.ensure_firewall("de-1-fw", "de-1")):
        with pytest.raises(CnsrError, match="不是 cnsr 创建的"):
            call()
    assert hz.delete_primary_ip("de-1-ip", "de-1") == ["Primary IP de-1-ip 不是 cnsr 创建的，未删除"]


def test_out_of_stock_creates_nothing():
    hz = FakeHetzner()
    hz.out_of_stock = CnsrError("无货")
    hz.ensure_primary_ip("de-1-ip", "de-1")
    with pytest.raises(CnsrError, match="无货"):
        hz.ensure_instance("de-1", "k", hz.pick("1c1g"), "de-1-ip")
    assert hz.servers == {} and hz.assigned == {"de-1-ip": None}


def test_a_server_that_is_off_never_counts_as_running():
    hz = FakeHetzner()
    hz.ensure_primary_ip("de-1-ip", "de-1")
    hz.ensure_instance("de-1", "k", hz.pick("1c1g"), "de-1-ip")
    hz.power_off("de-1")
    with pytest.raises(CnsrError, match="off"):
        hz.wait_running("de-1")
    hz.power_on("de-1")
    hz.wait_running("de-1")
