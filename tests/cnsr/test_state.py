import json
import random
import re

import pytest

from cnsr import naming
from cnsr.errors import CnsrError
from cnsr.state import ControlMeta, Inventory, Node, SshInfo, XrayInfo

META = ControlMeta(host="edge-cache-v123.example.com", zone="example.com", client_token="ct", admin_token="at")


def full_node(alias="jp-1") -> Node:
    return Node(
        alias=alias,
        source="aws",
        ipv4="192.0.2.10",
        ipv6="2001:db8::10",
        region="jp-tokyo",
        instance_name="cnsr-jp-1",
        static_ip_name="jp-1-ip",
        domain="jp-1.example.com",
        dns_record_ids={"A": "rec-a", "AAAA": "rec-aaaa"},
        ssh=SshInfo("admin", 22, "jp-1"),
        xray=XrayInfo("u", "pk", "0123456789abcdef", "www.example.com", "198.51.100.7"),
        node_token="nt",
        line="vless=192.0.2.10:443",
    )


# --- naming ---


def test_random_label_format():
    rng = random.Random(1)
    for _ in range(50):
        assert re.fullmatch(r"[a-z]+-[a-z]+-v\d{3}", naming.random_label(rng))


def test_random_label_words_come_from_the_list_and_differ():
    rng = random.Random(7)
    for _ in range(200):
        first, second, _ = naming.random_label(rng).split("-")
        assert first in naming.LABEL_WORDS and second in naming.LABEL_WORDS
        assert first != second


def test_random_label_is_deterministic_for_a_seed():
    assert naming.random_label(random.Random(5)) == naming.random_label(random.Random(5))


def test_label_words_are_the_agreed_list():
    assert naming.LABEL_WORDS == (
        "edge", "cache", "static", "media", "assets", "cdn", "api", "sync", "files", "metrics", "gateway", "data",
    )  # fmt: skip


@pytest.mark.parametrize(
    "alias,ok",
    [
        ("jp-1", True),
        ("us_node", True),
        ("0", True),
        ("a" * 32, True),
        ("Jp", False),
        ("-a", False),
        ("_a", False),
        ("a" * 33, False),
        ("", False),
        ("a b", False),
        ("a.b", False),
        ("东京", False),
        ("jp-1\n", False),
    ],
)
def test_validate_alias(alias, ok):
    assert (naming.validate_alias(alias) is None) == ok


def test_validate_alias_error_is_chinese():
    message = naming.validate_alias("Jp")
    assert message and re.search(r"[一-鿿]", message)


# --- node records ---


def test_node_json_round_trip():
    n = Node(
        alias="jp-1",
        source="aws",
        ipv4="192.0.2.10",
        ipv6="2001:db8::10",
        region="jp-tokyo",
        ssh=SshInfo("admin", 22, "jp-1"),
        xray=XrayInfo("u", "pk", "0123456789abcdef", "www.example.com", "198.51.100.7"),
    )
    assert Node.from_json(n.to_json()) == n


def test_node_json_round_trip_with_every_field():
    assert Node.from_json(full_node().to_json()) == full_node()


def test_node_json_round_trip_of_a_minimal_node():
    n = Node(alias="us_vps", source="vps", ipv4="203.0.113.5")
    assert Node.from_json(n.to_json()) == n


def test_node_to_json_is_flat_enough_for_the_worker():
    data = json.loads(full_node().to_json())
    assert data["alias"] == "jp-1"
    assert data["line"] == "vless=192.0.2.10:443"
    assert data["ssh"] == {"user": "admin", "port": 22, "key_name": "jp-1"}
    assert data["xray"]["short_id"] == "0123456789abcdef"
    assert data["dns_record_ids"] == {"A": "rec-a", "AAAA": "rec-aaaa"}


def test_node_to_json_writes_none_as_null():
    data = json.loads(Node(alias="a", source="vps", ipv4="192.0.2.1").to_json())
    assert data["ipv6"] is None and data["ssh"] is None and data["xray"] is None and data["line"] is None
    assert data["dns_record_ids"] == {}


def test_node_to_json_keeps_non_ascii_text_readable():
    node = Node(alias="a", source="vps", ipv4="192.0.2.1", line="东京 一")
    assert "东京 一" in node.to_json()
    assert Node.from_json(node.to_json()).line == "东京 一"


def test_node_from_json_ignores_unknown_keys():
    data = json.loads(full_node().to_json())
    data["added_later"] = 1
    data["ssh"]["extra"] = "x"
    data["xray"]["extra"] = "x"
    assert Node.from_json(json.dumps(data)) == full_node()


def test_node_from_json_fills_defaults_for_missing_optional_keys():
    node = Node.from_json('{"alias": "a", "source": "vps", "ipv4": "192.0.2.1"}')
    assert node == Node(alias="a", source="vps", ipv4="192.0.2.1")


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '{"alias": "a"}', '{"alias": "a", "source": "vps", "ipv4": "x", "ssh": "root"}'])
def test_node_from_json_rejects_bad_records_with_value_error(raw):
    with pytest.raises(ValueError):
        Node.from_json(raw)


# --- inventory ---


@pytest.mark.parametrize("raw", [None, '{"version": 1, "exhausted": false}', "garbage", "[]"])
def test_inventory_clear_exhausted_writes_nothing_without_an_exhausted_state(fake_kv, raw):
    if raw is not None:
        fake_kv.data["state:jp-1"] = raw
    before = dict(fake_kv.data)
    assert Inventory(fake_kv).clear_exhausted("jp-1") is False
    assert fake_kv.data == before


def test_inventory_put_replaces_an_existing_record(fake_kv):
    inventory = Inventory(fake_kv)
    inventory.put(full_node())
    changed = full_node()
    changed.line = "vless=198.51.100.9:443"
    inventory.put(changed)
    assert inventory.get("jp-1").line == "vless=198.51.100.9:443"
    assert sorted(fake_kv.data) == ["index:nodes", "node:jp-1"]


# --- the node index (index:nodes), which spares the Worker a KV list per status request ---


def index(fake_kv) -> list[str]:
    return json.loads(fake_kv.data["index:nodes"])


def test_inventory_put_adds_the_alias_to_the_index_once(fake_kv):
    inventory = Inventory(fake_kv)
    inventory.put(full_node("jp-2"))
    inventory.put(full_node("jp-1"))
    inventory.put(full_node("jp-1"))
    assert index(fake_kv) == ["jp-1", "jp-2"]


def test_inventory_put_writes_the_index_before_the_record(fake_kv):
    order = []
    put = fake_kv.put
    fake_kv.put = lambda key, value: (order.append(key), put(key, value))
    Inventory(fake_kv).put(full_node())
    assert order == ["index:nodes", "node:jp-1"]


def test_inventory_put_leaves_an_index_that_already_has_the_alias_alone(fake_kv):
    inventory = Inventory(fake_kv)
    inventory.put(full_node())
    written = []
    put = fake_kv.put
    fake_kv.put = lambda key, value: (written.append(key), put(key, value))
    inventory.put(full_node())
    assert written == ["node:jp-1"]


def test_inventory_aliases_reads_the_index_and_writes_nothing(fake_kv):
    fake_kv.data["node:old-1"] = full_node("old-1").to_json()
    before = dict(fake_kv.data)
    assert Inventory(fake_kv).aliases() == ["old-1"]
    assert fake_kv.data == before
    fake_kv.data["index:nodes"] = json.dumps(["b", "a"])
    assert Inventory(fake_kv).aliases() == ["a", "b"]


def test_inventory_keep_indexed_restores_an_alias_another_run_dropped(fake_kv):
    inventory = Inventory(fake_kv)
    inventory.put(full_node("jp-1"))
    fake_kv.data["index:nodes"] = json.dumps(["jp-2"])
    inventory.keep_indexed("jp-1")
    assert index(fake_kv) == ["jp-1", "jp-2"]


def test_inventory_delete_removes_the_record_then_the_alias(fake_kv):
    inventory = Inventory(fake_kv)
    inventory.put(full_node("jp-1"))
    inventory.put(full_node("jp-2"))
    seen = []
    delete = fake_kv.delete
    fake_kv.delete = lambda key: (seen.append((index(fake_kv), key)), delete(key))
    inventory.delete("jp-1")
    assert seen == [(["jp-1", "jp-2"], "node:jp-1")]
    assert index(fake_kv) == ["jp-2"] and "node:jp-1" not in fake_kv.data


def test_inventory_lists_the_namespace_only_while_there_is_no_index(fake_kv):
    """A control plane from before the index: its nodes must not drop out of the Worker's view."""
    fake_kv.data["node:old-1"] = full_node("old-1").to_json()
    fake_kv.data["node:old-2"] = full_node("old-2").to_json()
    inventory = Inventory(fake_kv)
    inventory.put(full_node("jp-1"))
    assert index(fake_kv) == ["jp-1", "old-1", "old-2"]
    inventory.put(full_node("jp-2"))
    inventory.delete("old-1")
    assert index(fake_kv) == ["jp-1", "jp-2", "old-2"]
    assert fake_kv.lists == 1


def test_inventory_delete_without_an_index_builds_one_from_the_records_left(fake_kv):
    fake_kv.data["node:old-1"] = full_node("old-1").to_json()
    fake_kv.data["node:old-2"] = full_node("old-2").to_json()
    Inventory(fake_kv).delete("old-1")
    assert index(fake_kv) == ["old-2"]


@pytest.mark.parametrize("raw", ["garbage", "{}", '["a", 1]'])
def test_inventory_rebuilds_a_damaged_index_from_the_records(fake_kv, raw):
    fake_kv.data["index:nodes"] = raw
    fake_kv.data["node:old-1"] = full_node("old-1").to_json()
    Inventory(fake_kv).put(full_node("jp-1"))
    assert index(fake_kv) == ["jp-1", "old-1"]


def test_inventory_meta_round_trip(fake_kv):
    inventory = Inventory(fake_kv)
    assert inventory.meta() is None
    inventory.put_meta(META)
    assert inventory.meta() == META
    assert json.loads(fake_kv.data["meta:control"]) == {
        "host": "edge-cache-v123.example.com",
        "zone": "example.com",
        "client_token": "ct",
        "admin_token": "at",
        "node_host": "",
    }


def test_inventory_meta_ignores_unknown_keys(fake_kv):
    data = {"host": "h.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at", "later": 1}
    fake_kv.data["meta:control"] = json.dumps(data)
    assert Inventory(fake_kv).meta() == ControlMeta("h.example.com", "example.com", "ct", "at")


@pytest.mark.parametrize("raw", ["garbage", "[]", '{"host": "h"}'])
def test_inventory_reports_corrupt_records_in_chinese(fake_kv, raw):
    fake_kv.data["meta:control"] = raw
    fake_kv.data["node:a"] = raw
    inventory = Inventory(fake_kv)
    for call in (inventory.meta, lambda: inventory.get("a")):
        with pytest.raises(CnsrError) as info:
            call()
        assert re.search(r"[一-鿿]", str(info.value))
    with pytest.raises(CnsrError, match="node:a"):
        inventory.get("a")
    with pytest.raises(CnsrError, match="meta:control"):
        inventory.meta()


def test_tokens_stay_out_of_reprs():
    meta = ControlMeta("c.example.com", "example.com", "client-secret-token", "admin-secret-token")
    node = Node(alias="jp-1", source="vps", ipv4="192.0.2.1", node_token="node-secret-token")
    for text in (repr(meta), repr(node), str(meta), str(node)):
        assert "secret-token" not in text
    assert "c.example.com" in repr(meta) and "jp-1" in repr(node)


def test_tokens_and_uuid_are_registered(monkeypatch):
    from cnsr import runlog

    registered = []
    monkeypatch.setattr(runlog, "secret", registered.append)
    ControlMeta("c.example.com", "example.com", "client-secret-token", "admin-secret-token")
    assert {"client-secret-token", "admin-secret-token"} <= set(registered)
    registered.clear()
    raw = json.dumps(
        {
            "alias": "jp-1",
            "source": "vps",
            "ipv4": "192.0.2.1",
            "node_token": "node-secret-token",
            "xray": {"uuid": "uuid-1234-5678", "public_key": "pub", "short_id": "ab", "sni": "example.com", "dest_ip": "192.0.2.2"},
        }
    )
    Node.from_json(raw)
    assert "node-secret-token" in registered and "uuid-1234-5678" in registered
    registered.clear()
    Node(alias="jp-1", source="vps", ipv4="192.0.2.1")  # no token yet: nothing to register
    assert [v for v in registered if v is not None] == []

