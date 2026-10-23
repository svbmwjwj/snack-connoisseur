"""Lightsail removal calls against botocore's Stubber (no network, no real AWS)."""

from __future__ import annotations

import pytest

from cnsr.aws.lightsail import LightsailError

pytestmark = pytest.mark.usefixtures("no_aws_environment")

GONE = ("NotFoundException",)
NOT_ATTACHED = ("InvalidInputException", "The static IP 'jp-1-ip' is not attached to an instance.")


def release(aws, *, detach=None, release=None) -> list[str]:
    for operation, outcome in (("detach_static_ip", detach), ("release_static_ip", release)):
        if outcome is None:
            aws.ok(operation, staticIpName="jp-1-ip")
        else:
            aws.fail(operation, *outcome, staticIpName="jp-1-ip")
    return aws.ls.release_static_ip("jp-1-ip")


def delete_instance(aws, outcome=None) -> list[str]:
    if outcome is None:
        aws.ok("delete_instance", instanceName="jp-1")
    else:
        aws.fail("delete_instance", *outcome, instanceName="jp-1")
    return aws.ls.delete_instance("jp-1")


# --- static_ip_attachment

def test_attachment_of_a_missing_ip(aws):
    aws.fail("get_static_ip", "NotFoundException", staticIpName="jp-1-ip")
    assert aws.ls.static_ip_attachment("jp-1-ip") == (False, None)


def test_attachment_of_a_free_ip(aws):
    aws.static_ip(isAttached=False)
    assert aws.ls.static_ip_attachment("jp-1-ip") == (True, None)


def test_attachment_names_the_instance(aws):
    aws.static_ip(isAttached=True, attachedTo="web")
    assert aws.ls.static_ip_attachment("jp-1-ip") == (True, "web")


def test_an_attached_ip_is_never_reported_as_free(aws):
    aws.static_ip(isAttached=True)
    exists, attached_to = aws.ls.static_ip_attachment("jp-1-ip")
    assert exists and attached_to


def test_attachment_does_not_hide_other_errors(aws):
    aws.fail("get_static_ip", "AccessDeniedException", staticIpName="jp-1-ip")
    with pytest.raises(LightsailError) as caught:
        aws.ls.static_ip_attachment("jp-1-ip")
    assert caught.value.operation == "get_static_ip"


# --- release_static_ip

def test_release_detaches_then_releases(aws):
    assert release(aws) == []


def test_release_ignores_what_is_already_gone(aws):
    assert release(aws, detach=GONE, release=GONE) == []


def test_release_ignores_a_static_ip_that_is_not_attached(aws):
    assert release(aws, detach=NOT_ATTACHED) == []


def test_release_still_releases_after_a_failed_detach(aws):
    errors = release(aws, detach=("InvalidInputException", "something else"))
    assert len(errors) == 1 and "detach_static_ip" in errors[0]


def test_release_reports_a_failed_release(aws):
    errors = release(aws, release=("AccessDeniedException",))
    assert len(errors) == 1
    assert "release_static_ip" in errors[0] and "释放静态 IP" in errors[0]


def test_an_invalid_input_error_on_release_is_not_ignored(aws):
    assert len(release(aws, release=("InvalidInputException", "The static IP is not attached"))) == 1


# --- delete_instance

def test_delete_instance(aws):
    assert delete_instance(aws) == []


def test_delete_instance_ignores_one_that_is_already_gone(aws):
    assert delete_instance(aws, GONE) == []


def test_delete_instance_reports_other_failures(aws):
    errors = delete_instance(aws, ("OperationFailureException",))
    assert len(errors) == 1 and "delete_instance" in errors[0] and "删除实例" in errors[0]
