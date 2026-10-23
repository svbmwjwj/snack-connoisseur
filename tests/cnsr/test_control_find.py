"""``find_control_plane``: looking at the control plane without creating or changing anything."""

import json
import random

import pytest

from cnsr.cloudflare import control
from cnsr.errors import CnsrError
from cnsr.state import ControlMeta

ZONE_ID = "zone-1"
EXISTING = {"host": "api-sync-v042.example.com", "zone": "example.com", "client_token": "ct", "admin_token": "at"}


def only_reads(mock_cf) -> bool:
    return {method for method, _ in mock_cf.calls()} == {"GET"}


def test_a_fresh_account_has_no_control_plane(mock_cf):
    assert control.find_control_plane(mock_cf.client, "example.com") is None
    assert only_reads(mock_cf) and mock_cf.namespaces == {}


def test_a_namespace_without_metadata_is_no_control_plane(mock_cf):
    mock_cf.add_namespace("cnsr-control")
    assert control.find_control_plane(mock_cf.client, "example.com") is None
    assert only_reads(mock_cf)


def test_other_namespaces_are_ignored(mock_cf):
    mock_cf.add_namespace("somebody-else")
    assert control.find_control_plane(mock_cf.client, "example.com") is None
    assert list(mock_cf.namespaces.values()) == ["somebody-else"]


def test_finds_the_deployed_control_plane_and_changes_nothing(mock_cf):
    deployed_kv, deployed_meta, _ = control.ensure_control_plane(mock_cf.client, "example.com", random.Random(1))
    mock_cf.requests.clear()

    found = control.find_control_plane(mock_cf.client, "example.com")

    assert found is not None
    kv, meta, zone_id = found
    assert (meta, zone_id) == (deployed_meta, ZONE_ID)
    assert kv.get("meta:control") == deployed_kv.get("meta:control")
    assert only_reads(mock_cf)


def test_a_control_plane_of_another_zone_is_refused(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    other = {**EXISTING, "host": "api-sync-v042.example.net", "zone": "example.net"}
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps(other)
    with pytest.raises(CnsrError, match="example.net 下，与当前设置的域名 example.com 不一致"):
        control.find_control_plane(mock_cf.client, "example.com")
    assert only_reads(mock_cf)


def test_zones_are_compared_ignoring_case_and_trailing_dot(mock_cf):
    namespace_id = mock_cf.add_namespace("cnsr-control")
    mock_cf.kv[namespace_id]["meta:control"] = json.dumps({**EXISTING, "zone": "Example.Com."})
    found = control.find_control_plane(mock_cf.client, "example.com")
    assert found is not None and found[1] == ControlMeta(EXISTING["host"], "Example.Com.", "ct", "at")


def test_an_unknown_zone_is_an_error(mock_cf):
    with pytest.raises(CnsrError, match="other.example.org"):
        control.find_control_plane(mock_cf.client, "other.example.org")
