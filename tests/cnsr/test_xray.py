"""Tests for the Xray identity, key parsing and config rendering helpers."""

from __future__ import annotations

import base64
import copy
import json
import random
import re
import secrets
import uuid
from importlib.resources import files

import pytest

from cnsr.errors import CnsrError
from cnsr.server.xray import (
    BACKUPS_PATH,
    COMPOSE_PATH,
    CONFIG_PATH,
    NODE_DIR,
    NODE_ENV_PATH,
    XRAY_IMAGE,
    compose_text,
    config_template,
    derive_public_key,
    new_identity,
    parse_x25519,
    read_identity,
    render_config,
)

ASSETS = files("cnsr.assets.xray")
UUID = "11111111-1111-4111-8111-111111111111"
RENDER = {
    "alias": "jp-1",
    "uuid": UUID,
    "private_key": "PRIV",
    "short_id": "0123456789abcdef",
    "sni": "www.example.com",
    "dest_ip": "198.51.100.7",
}


def template() -> dict:
    return json.loads(ASSETS.joinpath("config.json").read_text())


def test_render_config_fills_all_placeholders():
    out = json.loads(render_config(template(), **RENDER))
    ib = out["inbounds"][0]
    rs = ib["streamSettings"]["realitySettings"]
    assert ib["tag"] == "jp-1" and ib["port"] == 40443 and ib["settings"]["clients"][0]["id"].startswith("1111")
    assert rs["dest"] == "198.51.100.7:443" and rs["serverNames"] == ["www.example.com"]
    assert rs["shortIds"] == ["0123456789abcdef"] and rs["privateKey"] == "PRIV"
    assert "PLACEHOLDER" not in json.dumps(out)


def test_render_config_sets_the_client_email_from_the_alias():
    client = json.loads(render_config(template(), **RENDER))["inbounds"][0]["settings"]["clients"][0]
    assert client == {"id": UUID, "flow": "xtls-rprx-vision", "email": "jp-1@cnsr", "level": 0}


def test_render_config_changes_only_the_eight_fields():
    out = json.loads(render_config(template(), **RENDER))
    ib = out["inbounds"][0]
    rs = ib["streamSettings"]["realitySettings"]
    rs.update(dest="PLACEHOLDER_SNI:443", shortIds=["PLACEHOLDER_SHORT_ID"], privateKey="PLACEHOLDER_PRIVATE_KEY")
    rs["serverNames"] = ["PLACEHOLDER_SNI"]
    ib["tag"] = "PLACEHOLDER_ALIAS"
    ib["settings"]["clients"][0].update(id="PLACEHOLDER_UUID", email="user@example.com")
    assert out == template()


def test_render_config_leaves_the_template_untouched():
    tpl = template()
    before = copy.deepcopy(tpl)
    render_config(tpl, **RENDER)
    assert tpl == before


def test_render_config_text_format():
    text = render_config(template(), **RENDER)
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert text.startswith('{\n  "log": {\n    "loglevel"')
    assert "\r" not in text
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def test_render_config_does_not_escape_non_ascii():
    assert "节点" in render_config(template(), **{**RENDER, "alias": "节点"})


def test_config_template_is_the_bundled_file():
    assert config_template() == template()
    assert "PLACEHOLDER_UUID" in json.dumps(config_template())


@pytest.mark.parametrize(
    "text",
    ["PrivateKey: aaa\nPassword: bbb\nHash32: ccc\n", "Private key: aaa\nPublic key: bbb\n"],
)
def test_parse_x25519(text):
    assert parse_x25519(text) == ("aaa", "bbb")


@pytest.mark.parametrize(
    "text",
    [
        "PrivateKey: aaa\r\nPassword (PublicKey): bbb\r\nHash32: ccc\r\n",
        "\nWarning: pulling\n  PrivateKey:   aaa  \nPassword:bbb\n",
        "Hash32: ccc\nPassword: bbb\nPrivateKey: aaa",
    ],
)
def test_parse_x25519_tolerates_noise_crlf_and_order(text):
    assert parse_x25519(text) == ("aaa", "bbb")


@pytest.mark.parametrize(
    "text",
    ["", "PrivateKey: aaa\n", "Password: bbb\n", "PrivateKey:\nPassword: bbb\n", "Error: no such command\n"],
)
def test_parse_x25519_rejects_incomplete_output(text):
    with pytest.raises(CnsrError, match="x25519"):
        parse_x25519(text)


def test_read_identity_round_trip():
    text = render_config(template(), **RENDER)
    assert read_identity(text) == {
        "uuid": UUID,
        "private_key": "PRIV",
        "short_id": "0123456789abcdef",
        "sni": "www.example.com",
        "dest_ip": "198.51.100.7",
    }


def test_read_identity_tolerates_bom_and_crlf():
    text = "\ufeff" + render_config(template(), **RENDER).replace("\n", "\r\n")
    assert read_identity(text)["uuid"] == UUID


def _broken(change) -> str:
    cfg = json.loads(render_config(template(), **RENDER))
    change(cfg)
    return json.dumps(cfg)


def _drop_clients(cfg):
    cfg["inbounds"][0]["settings"]["clients"] = []


def _drop_reality(cfg):
    del cfg["inbounds"][0]["streamSettings"]["realitySettings"]


def _dest_without_port(cfg):
    cfg["inbounds"][0]["streamSettings"]["realitySettings"]["dest"] = "198.51.100.7"


def _dest_other_port(cfg):
    cfg["inbounds"][0]["streamSettings"]["realitySettings"]["dest"] = "198.51.100.7:8443"


def _empty_short_ids(cfg):
    cfg["inbounds"][0]["streamSettings"]["realitySettings"]["shortIds"] = []


def _numeric_key(cfg):
    cfg["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = 7


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"inbounds": []}',
        '{"inbounds": [1]}',
        _broken(_drop_clients),
        _broken(_drop_reality),
        _broken(_dest_without_port),
        _broken(_dest_other_port),
        _broken(_empty_short_ids),
        _broken(_numeric_key),
    ],
)
def test_read_identity_rejects_malformed_config(text):
    with pytest.raises(CnsrError, match="Xray 配置"):
        read_identity(text)


def test_new_identity_format():
    uid, sid = new_identity(random.Random(1))
    parsed = uuid.UUID(uid)
    assert parsed.version == 4 and parsed.variant == uuid.RFC_4122
    assert str(parsed) == uid
    assert re.fullmatch("[0-9a-f]{16}", sid)


def test_new_identity_is_deterministic_for_a_seeded_rng_and_varies_otherwise():
    assert new_identity(random.Random(7)) == new_identity(random.Random(7))
    assert new_identity(random.Random(7)) != new_identity(random.Random(8))


def test_new_identity_accepts_the_system_rng():
    uid, sid = new_identity(secrets.SystemRandom())
    assert uuid.UUID(uid).version == 4 and len(sid) == 16


def test_new_identity_keeps_leading_zeros_of_the_short_id():
    class Zero(random.Random):
        def getrandbits(self, k):
            return 1

    assert new_identity(Zero())[1] == "0000000000000001"


def test_constants():
    assert XRAY_IMAGE == "ghcr.io/xtls/xray-core:26.3.27"
    assert NODE_DIR == "/opt/cnsr"
    assert (COMPOSE_PATH, CONFIG_PATH) == ("/opt/cnsr/xray/compose.yml", "/opt/cnsr/xray/conf/config.json")
    assert (BACKUPS_PATH, NODE_ENV_PATH) == ("/opt/cnsr/backups.json", "/opt/cnsr/node.env")


def test_compose_uses_the_same_image_as_xray_image():
    images = re.findall(r"^\s*image:\s*(\S+)\s*$", compose_text(), re.MULTILINE)
    assert images == [XRAY_IMAGE]


def test_compose_text_is_the_bundled_file_with_lf_endings():
    text = compose_text()
    assert text == ASSETS.joinpath("compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "\r" not in text and not text.startswith("\ufeff")


def test_compose_keeps_the_logging_limits_and_the_ipv6_network():
    text = compose_text()
    assert 'max-size: "10m"' in text and 'max-file: "3"' in text
    assert "name: xray_dualstack" in text and "enable_ipv6: true" in text
    assert "./conf:/usr/local/etc/xray:ro" in text and '"443:40443/tcp"' in text


# RFC 7748, section 6.1: Alice's X25519 private and public key.
RFC_PRIVATE = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
RFC_PUBLIC = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")


def xray_base64(raw: bytes) -> str:
    """Keys the way xray prints and reads them: base64url without padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_derive_public_key_matches_rfc7748():
    assert derive_public_key(xray_base64(RFC_PRIVATE)) == xray_base64(RFC_PUBLIC)


def test_derive_public_key_of_a_clamped_private_key():
    # `xray x25519` prints the private key after clamping; the public key stays the same.
    clamped = bytearray(RFC_PRIVATE)
    clamped[0] &= 248
    clamped[31] = (clamped[31] & 127) | 64
    assert derive_public_key(xray_base64(bytes(clamped))) == xray_base64(RFC_PUBLIC)


@pytest.mark.parametrize("bad", ["", "PRIV", xray_base64(bytes(31)), xray_base64(RFC_PRIVATE) + "=", "+" * 43, "/" * 43])
def test_derive_public_key_rejects_malformed_keys(bad):
    with pytest.raises(CnsrError, match="私钥"):
        derive_public_key(bad)
