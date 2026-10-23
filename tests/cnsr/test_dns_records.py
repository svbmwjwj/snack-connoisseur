"""Tests for the node domain records and the SSH alias switch."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from cnsr.cloudflare.api import CloudflareError
from cnsr.errors import CnsrError
from cnsr.deploy.dns_records import delete_node_records, drop_aaaa, ensure_node_records, switch_alias_when_ready
from cnsr.naming import random_label
from cnsr.ssh.config import HostBlock, read_block, write_block
from cnsr.state import Node

ZONE = "example.com"
DOMAIN = "edge-cache-v123.example.com"


def dual_stack(**changes) -> Node:
    return Node(alias="jp-1", source="aws", ipv4="192.0.2.10", ipv6="2001:db8::10", **changes)


def ensure(fake_dns, node: Node, seed: int = 1) -> None:
    ensure_node_records(fake_dns.client, fake_dns.zone_id, ZONE, node, random.Random(seed))


def methods(fake_dns) -> list[str]:
    return [method for method, _ in fake_dns.calls()]


# ensure_node_records


def test_creates_a_and_aaaa(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    assert node.domain == f"{random_label(random.Random(1))}.{ZONE}"
    (a,), (aaaa,) = fake_dns.of_type("A"), fake_dns.of_type("AAAA")
    assert (a["name"], a["content"], a["ttl"], a["proxied"]) == (node.domain, "192.0.2.10", 60, False)
    assert (aaaa["name"], aaaa["content"], aaaa["ttl"], aaaa["proxied"]) == (node.domain, "2001:db8::10", 60, False)
    assert node.dns_record_ids == {"A": a["id"], "AAAA": aaaa["id"]}


def test_ipv4_only_creates_a(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10")
    ensure(fake_dns, node)
    assert fake_dns.of_type("AAAA") == []
    assert list(node.dns_record_ids) == ["A"]
    assert methods(fake_dns) == ["GET", "GET", "POST"]  # the name is free, no A record yet, create it


def test_a_new_domain_never_takes_a_name_that_already_has_records(fake_dns):
    rng = random.Random(1)
    taken, free = (f"{random_label(rng)}.{ZONE}" for _ in range(2))
    fake_dns.records["user"] = {"id": "user", "type": "CNAME", "name": taken, "content": "shop.example.com"}
    node = dual_stack()
    ensure(fake_dns, node)
    assert node.domain == free
    assert fake_dns.records["user"] == {"id": "user", "type": "CNAME", "name": taken, "content": "shop.example.com"}
    assert {record["name"] for record in fake_dns.of_type("A") + fake_dns.of_type("AAAA")} == {free}


class OneName:
    """An rng for ``random_label`` that always draws the same label, edge-cache-v000."""

    def sample(self, population, k):
        return list(population[:k])

    def randrange(self, stop):
        return 0


def test_a_new_domain_gives_up_when_every_name_drawn_is_taken(fake_dns):
    fake_dns.records["user"] = {"id": "user", "type": "TXT", "name": f"edge-cache-v000.{ZONE}", "content": "x"}
    node = dual_stack()
    with pytest.raises(CnsrError, match="已有解析记录"):
        ensure_node_records(fake_dns.client, fake_dns.zone_id, ZONE, node, OneName())
    assert node.domain is None and methods(fake_dns) == ["GET"] * 5


def test_records_name_the_node_in_their_comment(fake_dns):
    ensure(fake_dns, dual_stack())
    assert {r["comment"] for r in fake_dns.records.values()} == {"cnsr node jp-1"}


def test_rerun_adds_the_comment_to_records_made_without_one(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    for record in fake_dns.records.values():
        del record["comment"]
    ensure(fake_dns, node)
    assert {r["comment"] for r in fake_dns.records.values()} == {"cnsr node jp-1"}
    assert len(fake_dns.records) == 2


def test_rerun_keeps_domain(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10", domain=DOMAIN)
    ensure(fake_dns, node, seed=2)
    assert node.domain == DOMAIN
    assert fake_dns.of_type("A")[0]["name"] == DOMAIN


def test_rerun_reuses_records_without_writes(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    ids, domain = dict(node.dns_record_ids), node.domain
    fake_dns.requests.clear()
    ensure(fake_dns, node, seed=99)
    assert (node.domain, node.dns_record_ids) == (domain, ids)
    assert methods(fake_dns) == ["GET", "GET"]
    assert len(fake_dns.records) == 2


def test_rerun_updates_changed_address(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    a_id = node.dns_record_ids["A"]
    node.ipv4 = "192.0.2.11"
    ensure(fake_dns, node)
    assert node.dns_record_ids["A"] == a_id
    assert fake_dns.records[a_id]["content"] == "192.0.2.11"
    assert len(fake_dns.records) == 2


def test_rerun_without_ipv6_deletes_stored_aaaa(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    node.ipv6 = None
    ensure(fake_dns, node)
    assert fake_dns.of_type("AAAA") == []
    assert list(node.dns_record_ids) == ["A"]


def test_domain_is_remembered_when_cloudflare_fails(fake_dns):
    fake_dns.deny("POST", "A")
    node = dual_stack()
    with pytest.raises(CloudflareError):
        ensure(fake_dns, node)
    assert node.domain == f"{random_label(random.Random(1))}.{ZONE}"
    assert node.dns_record_ids == {}


def test_partial_failure_keeps_the_a_id(fake_dns):
    fake_dns.deny("POST", "AAAA")
    node = dual_stack()
    with pytest.raises(CloudflareError):
        ensure(fake_dns, node)
    assert node.domain
    assert list(node.dns_record_ids) == ["A"]
    fake_dns.allow("POST", "AAAA")
    ensure(fake_dns, node, seed=7)
    assert set(node.dns_record_ids) == {"A", "AAAA"}
    assert len(fake_dns.of_type("A")) == 1


# drop_aaaa


def test_drop_aaaa_removes_id(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    a_id = node.dns_record_ids["A"]
    drop_aaaa(fake_dns.client, fake_dns.zone_id, node)
    assert fake_dns.of_type("AAAA") == []
    assert node.dns_record_ids == {"A": a_id}
    assert fake_dns.records[a_id]["type"] == "A"


def test_drop_aaaa_is_a_noop_without_a_stored_id(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10", dns_record_ids={"A": "rec-1"})
    drop_aaaa(fake_dns.client, fake_dns.zone_id, node)
    assert fake_dns.requests == []
    assert node.dns_record_ids == {"A": "rec-1"}


def test_drop_aaaa_tolerates_a_record_that_is_already_gone(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10", dns_record_ids={"AAAA": "rec-404"})
    drop_aaaa(fake_dns.client, fake_dns.zone_id, node)
    assert node.dns_record_ids == {}


def test_drop_aaaa_keeps_the_id_when_deleting_fails(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    fake_dns.deny("DELETE", "AAAA")
    with pytest.raises(CloudflareError):
        drop_aaaa(fake_dns.client, fake_dns.zone_id, node)
    assert "AAAA" in node.dns_record_ids


# delete_node_records


def test_delete_node_records_removes_everything(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    assert delete_node_records(fake_dns.client, fake_dns.zone_id, node) == []
    assert fake_dns.records == {}
    assert node.dns_record_ids == {}


def test_delete_node_records_without_records_does_nothing(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10")
    assert delete_node_records(fake_dns.client, fake_dns.zone_id, node) == []
    assert fake_dns.requests == []


def test_delete_node_records_collects_errors_and_keeps_going(fake_dns):
    node = dual_stack()
    ensure(fake_dns, node)
    a_id, aaaa_id = node.dns_record_ids["A"], node.dns_record_ids["AAAA"]
    fake_dns.deny("DELETE", "A")
    errors = delete_node_records(fake_dns.client, fake_dns.zone_id, node)
    assert len(errors) == 1
    assert node.domain in errors[0] and " A 记录" in errors[0] and "HTTP 500" in errors[0]
    assert set(fake_dns.records) == {a_id}
    assert node.dns_record_ids == {"A": a_id}
    assert aaaa_id not in fake_dns.records


def test_delete_node_records_counts_a_missing_record_as_deleted(fake_dns):
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10", dns_record_ids={"A": "rec-404"})
    assert delete_node_records(fake_dns.client, fake_dns.zone_id, node) == []
    assert node.dns_record_ids == {}


# switch_alias_when_ready


class Sleeps:
    """A ``sleep`` that only records how long it was asked to wait, and the clock it moves on."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)

    def now(self) -> float:
        return self.total

    def seams(self) -> dict:
        return {"sleep": self, "clock": self.now}


def alias_config(tmp_path: Path, hostname: str = "192.0.2.10") -> Path:
    path = tmp_path / "config"
    write_block(path, HostBlock("jp-1", hostname, "admin", 2222, "~/.ssh/pub/jp-1.pub"))
    return path


def switch_node(**changes) -> Node:
    return Node(alias="jp-1", source="aws", ipv4="192.0.2.10", domain=DOMAIN, **changes)


def test_switch_times_out_without_editing(tmp_path):
    path = alias_config(tmp_path)
    before, sleeps, logins = path.read_bytes(), Sleeps(), []
    result = switch_alias_when_ready(
        switch_node(), path, lambda host: logins.append(host) or True, lambda name, expected: False, **sleeps.seams()
    )
    assert result is False
    assert path.read_bytes() == before
    assert not (tmp_path / "config_bak_cnsr").exists()
    assert read_block(path, "jp-1").hostname == "192.0.2.10"
    assert sleeps.total == 120 and set(sleeps.calls) == {10}
    assert logins == []


def test_switch_edits_hostname_when_ready(tmp_path):
    path = alias_config(tmp_path)
    node, sleeps = switch_node(), Sleeps()
    assert switch_alias_when_ready(node, path, lambda host: True, lambda name, expected: True, **sleeps.seams()) is True
    assert read_block(path, "jp-1").hostname == node.domain
    assert sleeps.calls == []


def test_switch_keeps_every_other_field(tmp_path):
    path = alias_config(tmp_path)
    path.write_text("# mine\nHost prod\n    HostName 198.51.100.7\n\n" + path.read_text())
    switch_alias_when_ready(switch_node(), path, lambda host: True, lambda name, expected: True, **Sleeps().seams())
    assert read_block(path, "jp-1") == HostBlock("jp-1", DOMAIN, "admin", 2222, "~/.ssh/pub/jp-1.pub")
    assert read_block(path, "prod").hostname == "198.51.100.7"
    assert path.read_text().startswith("# mine\n")


def test_switch_checks_the_domain_against_both_addresses(tmp_path):
    path = alias_config(tmp_path)
    asked = []
    switch_alias_when_ready(
        switch_node(ipv6="2001:db8::10"),
        path,
        lambda host: True,
        lambda name, expected: asked.append((name, expected)) or True,
        **Sleeps().seams(),
    )
    assert asked == [(DOMAIN, {"192.0.2.10", "2001:db8::10"})]


def test_switch_expects_only_ipv4_on_an_ipv4_only_node(tmp_path):
    asked = []
    switch_alias_when_ready(
        switch_node(), alias_config(tmp_path), lambda host: True, lambda n, e: asked.append(e) or True, **Sleeps().seams()
    )
    assert asked == [{"192.0.2.10"}]


def test_switch_waits_until_the_name_resolves(tmp_path):
    path = alias_config(tmp_path)
    rounds, sleeps = [], Sleeps()

    def resolves(name, expected):
        rounds.append(name)
        return len(rounds) == 4

    assert switch_alias_when_ready(switch_node(), path, lambda host: True, resolves, **sleeps.seams()) is True
    assert sleeps.calls == [10, 10, 10]
    assert read_block(path, "jp-1").hostname == DOMAIN


def test_switch_needs_the_login_to_work_too(tmp_path):
    path = alias_config(tmp_path)
    before, logins, sleeps = path.read_bytes(), [], Sleeps()
    result = switch_alias_when_ready(
        switch_node(), path, lambda host: logins.append(host) or False, lambda n, e: True, **sleeps.seams()
    )
    assert result is False
    assert path.read_bytes() == before
    assert set(logins) == {DOMAIN} and len(logins) == 13
    assert sleeps.total == 120


def test_switch_waits_until_the_login_works(tmp_path):
    path = alias_config(tmp_path)
    logins = []

    def can_login(host):
        logins.append(host)
        return len(logins) == 2

    assert switch_alias_when_ready(switch_node(), path, can_login, lambda n, e: True, **Sleeps().seams()) is True
    assert read_block(path, "jp-1").hostname == DOMAIN


def test_switch_honours_a_shorter_wait(tmp_path):
    sleeps = Sleeps()
    result = switch_alias_when_ready(
        switch_node(), alias_config(tmp_path), lambda h: True, lambda n, e: False, wait=25, **sleeps.seams()
    )
    assert result is False
    assert sleeps.calls == [10, 10, 5]


def test_switch_does_nothing_when_the_alias_already_uses_the_domain(tmp_path):
    path = alias_config(tmp_path, hostname=DOMAIN)
    before, sleeps = path.read_bytes(), Sleeps()

    def fail(*args):
        raise AssertionError("must not be called")

    assert switch_alias_when_ready(switch_node(), path, fail, fail, **sleeps.seams()) is True
    assert path.read_bytes() == before
    assert sleeps.calls == []


def test_switch_without_an_alias_block_writes_nothing(tmp_path):
    path = tmp_path / "config"
    assert switch_alias_when_ready(switch_node(), path, lambda h: True, lambda n, e: True, **Sleeps().seams()) is False
    assert not path.exists()


def test_switch_ignores_a_block_without_a_hostname(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host jp-1\n    User admin\n")
    before = path.read_bytes()
    assert switch_alias_when_ready(switch_node(), path, lambda h: True, lambda n, e: True, **Sleeps().seams()) is False
    assert path.read_bytes() == before


def test_switch_without_a_domain_writes_nothing(tmp_path):
    path = alias_config(tmp_path)
    before = path.read_bytes()
    node = Node(alias="jp-1", source="aws", ipv4="192.0.2.10")
    assert switch_alias_when_ready(node, path, lambda h: True, lambda n, e: True, **Sleeps().seams()) is False
    assert path.read_bytes() == before
