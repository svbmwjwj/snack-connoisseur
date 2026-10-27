"""Lightsail operations against botocore's Stubber (no network, no real AWS)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore import exceptions as boto_exc

from cnsr.aws import lightsail
from cnsr.aws.lightsail import KeyConflict, Lightsail, LightsailError
from cnsr.errors import CnsrError
from key_samples import ED, ED_FINGERPRINT, RSA, RSA_FINGERPRINT
from lightsail_harness import REGION

pytestmark = pytest.mark.usefixtures("no_aws_environment")

FIREWALL = [
    {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]},
    {"fromPort": 443, "toPort": 443, "protocol": "tcp", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]},
    {"fromPort": 443, "toPort": 443, "protocol": "udp", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]},
]


def test_regions_and_defaults_are_fixed():
    assert lightsail.REGIONS == {"jp": "ap-northeast-1", "sg": "ap-southeast-1", "us": "us-west-2"}
    assert (lightsail.BUNDLE, lightsail.BLUEPRINTS, lightsail.LOGIN_USER) == (
        "nano_3_0", ("debian_13", "debian_12"), "admin")
    assert issubclass(KeyConflict, CnsrError) and issubclass(LightsailError, CnsrError)


# --- key pairs

def test_reuses_key_pair_with_same_fingerprint_any_name(aws):
    aws.pairs(("other", "x"), ("old-key", ED_FINGERPRINT))
    assert aws.ls.ensure_key_pair("jp-1", ED) == "old-key"


def test_matching_ignores_case_and_surrounding_whitespace(aws):
    aws.pairs(("old-key", f" {RSA_FINGERPRINT.upper()}\n"))
    assert aws.ls.ensure_key_pair("jp-1", RSA) == "old-key"


def test_a_same_name_match_wins_over_matches_under_other_names(aws):
    aws.pairs(("first", ED_FINGERPRINT), ("jp-1", ED_FINGERPRINT))
    assert aws.ls.ensure_key_pair("jp-1", ED) == "jp-1"


def test_a_match_under_another_name_beats_a_same_name_conflict(aws):
    aws.pairs(("jp-1", "x"), ("old-key", ED_FINGERPRINT))
    assert aws.ls.ensure_key_pair("jp-1", ED) == "old-key"


def test_same_name_different_fingerprint_conflicts(aws):
    aws.pairs(("jp-1", "x"))
    with pytest.raises(KeyConflict, match="jp-1") as caught:
        aws.ls.ensure_key_pair("jp-1", ED)
    assert REGION in str(caught.value)


def test_imports_when_missing(aws):
    aws.pairs(("other", "x"))
    aws.ok("import_key_pair", keyPairName="jp-1", publicKeyBase64=ED)
    assert aws.ls.ensure_key_pair("jp-1", ED) == "jp-1"


def test_the_public_key_is_sent_clean_of_bom_and_newline(aws):
    aws.pairs()
    aws.ok("import_key_pair", keyPairName="jp-1", publicKeyBase64=ED)
    assert aws.ls.ensure_key_pair("jp-1", "\ufeff" + ED + "\r\n") == "jp-1"


def test_key_pairs_are_read_page_by_page(aws):
    aws.pairs(("a", "x"), token="t1")
    aws.pairs(("b", "y"), token="t2", pageToken="t1")
    aws.pairs(("old-key", ED_FINGERPRINT), pageToken="t2")
    assert aws.ls.ensure_key_pair("jp-1", ED) == "old-key"


def test_a_conflict_on_a_later_page_is_still_found(aws):
    aws.pairs(("a", "x"), token="t1")
    aws.pairs(("jp-1", "y"), pageToken="t1")
    with pytest.raises(KeyConflict):
        aws.ls.ensure_key_pair("jp-1", ED)


def test_the_default_key_pair_is_never_selected(aws):
    aws.pairs(("LightsailDefaultKeyPair", ED_FINGERPRINT))
    aws.ok("import_key_pair", keyPairName="jp-1", publicKeyBase64=ED)
    assert aws.ls.ensure_key_pair("jp-1", ED) == "jp-1"


def test_the_default_key_pair_name_is_never_imported(aws):
    with pytest.raises(CnsrError, match="LightsailDefaultKeyPair"):
        aws.ls.ensure_key_pair("LightsailDefaultKeyPair", ED)


def test_a_bad_public_key_fails_before_any_call(aws):
    with pytest.raises(CnsrError, match="公钥"):
        aws.ls.ensure_key_pair("jp-1", "ssh-ed25519 AAAA")


# --- blueprints

@pytest.mark.parametrize("listed,expected", [
    (["debian_12", "debian_13", "ubuntu_24_04"], "debian_13"),
    (["ubuntu_24_04", "debian_12"], "debian_12"),
])
def test_pick_blueprint_prefers_debian_13(aws, listed, expected):
    aws.ok("get_blueprints", {"blueprints": [{"blueprintId": b, "isActive": True} for b in listed]})
    assert aws.ls.pick_blueprint() == expected


def test_pick_blueprint_skips_inactive_ones_and_reads_every_page(aws):
    aws.ok("get_blueprints", {"blueprints": [{"blueprintId": "debian_13", "isActive": False}], "nextPageToken": "t1"})
    aws.ok("get_blueprints", {"blueprints": [{"blueprintId": "debian_12", "isActive": True}]}, pageToken="t1")
    assert aws.ls.pick_blueprint() == "debian_12"


def test_pick_blueprint_without_debian_is_an_error(aws):
    aws.ok("get_blueprints", {"blueprints": [{"blueprintId": "ubuntu_24_04", "isActive": True}]})
    with pytest.raises(CnsrError, match="Debian"):
        aws.ls.pick_blueprint()


# --- instances

def zones(*states: str, name: str = REGION) -> dict:
    letters = "abcdef"
    return {"regions": [{"name": name, "availabilityZones": [
        {"zoneName": f"{name}{letters[i]}", "state": s} for i, s in enumerate(states)]}]}


def create_instance(aws, regions: dict, zone: str, blueprints=("debian_13",)) -> None:
    aws.fail("get_instance", "NotFoundException", instanceName="jp-1")
    aws.ok("get_regions", regions, includeAvailabilityZones=True)
    aws.ok("get_blueprints", {"blueprints": [{"blueprintId": b, "isActive": True} for b in blueprints]})
    aws.ok("create_instances", instanceNames=["jp-1"], availabilityZone=zone, blueprintId=blueprints[0],
           bundleId="nano_3_0", keyPairName="jp-1", ipAddressType="dualstack",
           tags=[{"key": "cnsr", "value": "jp-1"}])


def test_ensure_instance_creates_dualstack(aws):
    create_instance(aws, zones("available"), "ap-northeast-1a")
    assert aws.ls.ensure_instance("jp-1", "jp-1") is True  # created in this call


@pytest.mark.parametrize("regions,zone", [
    (zones("unavailable", "available", "available"), "ap-northeast-1b"),
    (zones("unavailable"), "ap-northeast-1a"),
    ({"regions": []}, "ap-northeast-1a"),
    (zones("available", name="us-west-2"), "ap-northeast-1a"),
])
def test_the_zone_is_the_first_available_one_of_the_region(aws, regions, zone):
    create_instance(aws, regions, zone, blueprints=("debian_12",))
    aws.ls.ensure_instance("jp-1", "jp-1")


def test_ensure_instance_reuses_tagged(aws):
    aws.instance(tags=[{"key": "other", "value": "x"}, {"key": "cnsr", "value": "jp-1"}])
    assert aws.ls.ensure_instance("jp-1", "jp-1") is False


@pytest.mark.parametrize("fields", [{}, {"tags": []}, {"tags": [{"key": "cnsr", "value": "jp-2"}]},
                                    {"tags": [{"key": "cnsr"}]}, {"tags": [{"key": "env", "value": "jp-1"}]}])
def test_ensure_instance_refuses_untagged(aws, fields):
    aws.instance(**fields)
    with pytest.raises(CnsrError, match="同名实例") as caught:
        aws.ls.ensure_instance("jp-1", "jp-1")
    assert "jp-1" in str(caught.value)


def test_instance_tagged_is_true_for_the_instance_cnsr_made(aws):
    aws.instance(tags=[{"key": "other", "value": "x"}, {"key": "cnsr", "value": "jp-1"}])
    assert aws.ls.instance_tagged("jp-1", "jp-1") is True


@pytest.mark.parametrize("fields", [{}, {"tags": []}, {"tags": [{"key": "cnsr", "value": "jp-2"}]},
                                    {"tags": [{"key": "cnsr"}]}, {"tags": [{"key": "env", "value": "jp-1"}]}])
def test_instance_tagged_is_false_for_a_strangers_instance(aws, fields):
    aws.instance(**fields)
    assert aws.ls.instance_tagged("jp-1", "jp-1") is False


def test_instance_tagged_is_none_when_there_is_no_such_instance(aws):
    aws.fail("get_instance", "NotFoundException", instanceName="jp-1")
    assert aws.ls.instance_tagged("jp-1", "jp-1") is None


def test_instance_tagged_does_not_hide_other_errors(aws):
    aws.fail("get_instance", "AccessDeniedException", instanceName="jp-1")
    with pytest.raises(LightsailError) as caught:
        aws.ls.instance_tagged("jp-1", "jp-1")
    assert caught.value.operation == "get_instance"


def test_wait_running_polls_every_five_seconds(aws):
    for state in ("pending", "pending", "running"):
        aws.ok("get_instance_state", {"state": {"code": 0, "name": state}}, instanceName="jp-1")
    aws.ls.wait_running("jp-1")
    assert aws.sleeps == [5, 5]


def test_wait_running_waits_through_the_none_state_right_after_create(aws):
    for state in ("none", "pending", "running"):
        aws.ok("get_instance_state", {"state": {"code": 0, "name": state}}, instanceName="jp-1")
    aws.ls.wait_running("jp-1")
    assert aws.sleeps == [5, 5]


def test_wait_running_gives_up_after_the_timeout(aws):
    for _ in range(3):
        aws.ok("get_instance_state", {"state": {"code": 0, "name": "pending"}}, instanceName="jp-1")
    with pytest.raises(CnsrError, match="10 秒"):
        aws.ls.wait_running("jp-1", timeout=10)
    assert aws.sleeps == [5, 5]


def test_wait_running_never_sleeps_past_the_timeout(aws):
    for _ in range(3):
        aws.ok("get_instance_state", {"state": {"code": 0, "name": "pending"}}, instanceName="jp-1")
    with pytest.raises(CnsrError):
        aws.ls.wait_running("jp-1", timeout=7)
    assert aws.sleeps == [5, 2]


def test_wait_running_counts_the_time_its_calls_take(aws):
    aws.call_seconds = 5  # as long as the pause between them: three calls fit in 20 seconds
    for _ in range(3):
        aws.ok("get_instance_state", {"state": {"code": 0, "name": "pending"}}, instanceName="jp-1")
    with pytest.raises(CnsrError, match="20 秒"):
        aws.ls.wait_running("jp-1", timeout=20)
    assert aws.now <= 20 + aws.call_seconds


@pytest.mark.parametrize("state", ["stopped", "stopping", "terminated", "shutting-down"])
def test_wait_running_stops_at_once_on_other_states(aws, state):
    aws.ok("get_instance_state", {"state": {"code": 80, "name": state}}, instanceName="jp-1")
    with pytest.raises(CnsrError, match=state):
        aws.ls.wait_running("jp-1")
    assert aws.sleeps == []


# --- static ip

def test_ensure_static_ip_allocates_attaches_and_rereads(aws):
    aws.fail("get_static_ip", "NotFoundException", staticIpName="jp-1-ip")
    aws.ok("allocate_static_ip", staticIpName="jp-1-ip")
    aws.ok("attach_static_ip", staticIpName="jp-1-ip", instanceName="jp-1")
    aws.static_ip("192.0.2.10", isAttached=True, attachedTo="jp-1")
    assert aws.ls.ensure_static_ip("jp-1", "jp-1-ip") == "192.0.2.10"


def test_ensure_static_ip_attaches_an_existing_free_one(aws):
    aws.static_ip("192.0.2.11", isAttached=False)
    aws.ok("attach_static_ip", staticIpName="jp-1-ip", instanceName="jp-1")
    aws.static_ip("192.0.2.11", isAttached=True, attachedTo="jp-1")
    assert aws.ls.ensure_static_ip("jp-1", "jp-1-ip") == "192.0.2.11"


def test_ensure_static_ip_keeps_one_already_attached_to_the_instance(aws):
    aws.static_ip("192.0.2.12", isAttached=True, attachedTo="jp-1")
    assert aws.ls.ensure_static_ip("jp-1", "jp-1-ip") == "192.0.2.12"


def test_ensure_static_ip_refuses_one_attached_elsewhere(aws):
    aws.static_ip(isAttached=True, attachedTo="jp-2")
    with pytest.raises(CnsrError, match="jp-2"):
        aws.ls.ensure_static_ip("jp-1", "jp-1-ip")


def test_ensure_static_ip_without_an_address_is_an_error(aws):
    aws.static_ip(isAttached=True, attachedTo="jp-1", ipAddress="")
    with pytest.raises(CnsrError, match="jp-1-ip"):
        aws.ls.ensure_static_ip("jp-1", "jp-1-ip")


# --- ipv6 and firewall

@pytest.mark.parametrize("fields,expected", [
    ({"ipv6Addresses": ["2001:db8::1", "2001:db8::2"]}, "2001:db8::1"),
    ({"ipv6Addresses": []}, None),
    ({}, None),
])
def test_instance_ipv6_is_the_first_address_or_none(aws, fields, expected):
    aws.instance(**fields)
    assert aws.ls.instance_ipv6("jp-1") == expected


def test_set_firewall_exact_ports(aws):
    aws.ok("put_instance_public_ports", instanceName="jp-1", portInfos=FIREWALL)
    aws.ls.set_firewall("jp-1")


# --- error mapping

def test_aws_errors_become_chinese_messages_that_name_the_operation(aws):
    aws.fail("get_instance", "AccessDeniedException", "arn:aws:iam::SECRET-DETAIL", instanceName="jp-1")
    with pytest.raises(LightsailError) as caught:
        aws.ls.instance_ipv6("jp-1")
    text = str(caught.value)
    assert "查询实例" in text and "get_instance" in text and REGION in text and "权限" in text
    assert "SECRET-DETAIL" not in text
    assert caught.value.code == "AccessDeniedException"


@pytest.mark.parametrize("code,fragment", [
    ("UnrecognizedClientException", "凭证"),
    ("InvalidSignatureException", "凭证"),
    ("ExpiredTokenException", "凭证"),
    ("ThrottlingException", "稍后"),
    ("NotFoundException", "不存在"),
    ("RegionSetupInProgressException", "初始化"),
])
def test_known_codes_get_fixed_chinese_reasons_without_the_aws_text(aws, code, fragment):
    aws.fail("get_instance", code, "SECRET-DETAIL", instanceName="jp-1")
    with pytest.raises(CnsrError, match=fragment) as caught:
        aws.ls.instance_ipv6("jp-1")
    assert "SECRET-DETAIL" not in str(caught.value)


def test_unknown_codes_show_the_code_and_the_aws_message(aws):
    aws.fail("get_instance", "InvalidInputException", "Invalid bundle", instanceName="jp-1")
    with pytest.raises(CnsrError, match="InvalidInputException：Invalid bundle"):
        aws.ls.instance_ipv6("jp-1")


@pytest.mark.parametrize("raised,fragment", [
    (boto_exc.NoCredentialsError(), "凭证"),
    (boto_exc.PartialCredentialsError(provider="env", cred_var="aws_secret_access_key"), "凭证"),
    (boto_exc.EndpointConnectionError(endpoint_url="https://lightsail.example.com"), "无法连接"),
    (boto_exc.ConnectTimeoutError(endpoint_url="https://lightsail.example.com"), "无法连接"),
    (boto_exc.ReadTimeoutError(endpoint_url="https://lightsail.example.com"), "无法连接"),
    (boto_exc.ParamValidationError(report="SECRET-DETAIL"), "ParamValidationError"),
])
def test_sdk_errors_become_chinese_messages_without_their_text(raised, fragment):
    def fail(**_):
        raise raised

    ls = Lightsail("jp", client=SimpleNamespace(get_instance=fail))
    with pytest.raises(LightsailError, match=fragment) as caught:
        ls.instance_ipv6("jp-1")
    text = str(caught.value)
    assert "get_instance" in text and "SECRET-DETAIL" not in text and "example.com" not in text
