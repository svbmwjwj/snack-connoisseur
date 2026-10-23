"""Making the Lightsail client: region codes and broken AWS profiles or config."""

from __future__ import annotations

import re

import pytest
from botocore import exceptions as boto_exc

from cnsr.aws import lightsail
from cnsr.aws.lightsail import Lightsail, LightsailError
from cnsr.errors import CnsrError
from lightsail_harness import REGION

pytestmark = pytest.mark.usefixtures("no_aws_environment")


def test_unknown_region_is_refused_before_any_client_exists(monkeypatch):
    monkeypatch.setattr(lightsail.boto3, "client", lambda *a, **k: pytest.fail("client created"))
    with pytest.raises(CnsrError, match="不支持的区域 eu") as caught:
        Lightsail("eu")
    assert "jp" in str(caught.value)


SDK_FAILURES = [
    (boto_exc.ProfileNotFound(profile="does-not-exist"), "AWS_PROFILE"),
    (boto_exc.InvalidConfigError(error_msg="SECRET-DETAIL"), "AWS_PROFILE"),
    (boto_exc.ConfigNotFound(path="/nowhere/config"), "AWS_PROFILE"),
    (boto_exc.ConfigParseError(path="/nowhere/config"), "AWS_PROFILE"),
    (boto_exc.UnknownCredentialError(name="SECRET-DETAIL"), "AWS_PROFILE"),
    (boto_exc.PartialCredentialsError(provider="env", cred_var="aws_secret_access_key"), "AWS_PROFILE"),
    (boto_exc.NoCredentialsError(), "凭证"),
    (boto_exc.CredentialRetrievalError(provider="env", error_msg="SECRET-DETAIL"), "凭证"),
    (boto_exc.SSOTokenLoadError(error_msg="SECRET-DETAIL"), "凭证"),
]
ENGLISH_SDK_TEXT = ("SECRET-DETAIL", "does-not-exist", "could not be found", "nowhere", "Unable to locate")


@pytest.mark.parametrize("raised,fragment", SDK_FAILURES)
def test_a_broken_profile_or_config_while_making_the_client_is_a_chinese_error(monkeypatch, raised, fragment):
    def fail(*_, **__):
        raise raised

    monkeypatch.setattr(lightsail.boto3, "client", fail)
    with pytest.raises(LightsailError, match=fragment) as caught:
        Lightsail("jp")
    text = str(caught.value)
    assert "创建 AWS 客户端" in text and REGION in text
    assert not [part for part in ENGLISH_SDK_TEXT if part in text]


def test_a_missing_aws_profile_in_the_environment_is_a_chinese_error(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    with pytest.raises(CnsrError, match="AWS_PROFILE") as caught:
        Lightsail("jp")
    assert "could not be found" not in str(caught.value)


@pytest.mark.parametrize("code,region", [("jp", "ap-northeast-1"), ("sg", "ap-southeast-1"), ("us", "us-west-2")])
def test_without_a_client_a_boto3_lightsail_client_is_made_for_the_region(monkeypatch, code, region):
    made = []
    monkeypatch.setattr(lightsail.boto3, "client", lambda *a, **k: made.append((a, k)) or object())
    Lightsail(code)
    assert made == [(("lightsail",), {"region_name": region})]


def test_lightsail_calls_are_logged_with_the_error_code(caplog):
    from lightsail_harness import Aws

    caplog.set_level("INFO", logger="cnsr")
    aws = Aws()
    aws.instance()
    aws.fail("get_instance", "NotFoundException", instanceName="jp-1")
    with aws.stubber:
        aws.ls._call("get_instance", instanceName="jp-1")
        with pytest.raises(LightsailError):
            aws.ls._call("get_instance", instanceName="jp-1")
    lines = [r.getMessage() for r in caplog.records if r.name == "cnsr.aws.lightsail"]
    assert len(lines) == 2
    assert "get_instance" in lines[0] and "成功" in lines[0]
    assert "get_instance" in lines[1] and "NotFoundException" in lines[1]
    assert all(re.search(r"，耗时 \d+\.\d 秒$", line) for line in lines)
