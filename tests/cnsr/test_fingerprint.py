"""Lightsail key pair fingerprints of SSH public keys."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cnsr.aws.fingerprint import lightsail_fingerprint
from cnsr.errors import CnsrError
from key_samples import ED, ED_FINGERPRINT, RSA, RSA_FINGERPRINT


def test_ed25519_fingerprint():
    assert lightsail_fingerprint(ED) == ED_FINGERPRINT


def test_rsa_fingerprint():
    assert lightsail_fingerprint(RSA) == RSA_FINGERPRINT


@pytest.mark.parametrize("decorate", [
    lambda line: line + "\n",
    lambda line: line + "\r\n",
    lambda line: "\ufeff" + line,
    lambda line: "  " + line + "  ",
    lambda line: line.rsplit(" ", 1)[0],
    lambda line: line + " a comment with spaces",
])
@pytest.mark.parametrize("line,expected", [(ED, ED_FINGERPRINT), (RSA, RSA_FINGERPRINT)])
def test_surrounding_whitespace_bom_and_comment_do_not_change_the_fingerprint(decorate, line, expected):
    assert lightsail_fingerprint(decorate(line)) == expected


def test_ed25519_fingerprint_keeps_the_padding():
    assert lightsail_fingerprint(ED).endswith("=")


def test_ecdsa_is_refused_with_a_chinese_message():
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    line = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    with pytest.raises(CnsrError, match="ED25519 和 RSA") as caught:
        lightsail_fingerprint(line)
    assert "ecdsa-sha2-nistp256" in str(caught.value)
    assert line.split()[1] not in str(caught.value)


@pytest.mark.parametrize("bad", [
    "",
    "   \n",
    "ssh-ed25519",
    "ssh-ed25519 !!!not-base64!!!",
    "ssh-ed25519 AAAA",
    "ssh-ed25519 ÄÄÄÄ",
    ED.replace("ssh-ed25519", "ssh-rsa", 1),
    RSA.replace("ssh-rsa", "ssh-ed25519", 1),
    ED + "\n" + RSA,
])
def test_malformed_lines_raise_a_chinese_error(bad):
    with pytest.raises(CnsrError) as caught:
        lightsail_fingerprint(bad)
    assert "公钥" in str(caught.value)


def test_ed25519_blob_with_trailing_bytes_is_malformed():
    blob = base64.b64decode(ED.split()[1]) + b"\x00"
    with pytest.raises(CnsrError, match="公钥"):
        lightsail_fingerprint("ssh-ed25519 " + base64.b64encode(blob).decode())
