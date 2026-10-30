"""Hetzner SSH key fingerprints: the MD5 of the key blob."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cnsr.aws.fingerprint import md5_fingerprint
from cnsr.errors import CnsrError
from key_samples import ED, ED_MD5, RSA, RSA_MD5


@pytest.mark.parametrize("line,expected", [(ED, ED_MD5), (RSA, RSA_MD5)])
def test_matches_ssh_keygen_md5(line, expected):
    assert md5_fingerprint(line) == expected


@pytest.mark.parametrize("decorate", [
    lambda line: line + "\r\n",
    lambda line: "\ufeff  " + line,
    lambda line: line.rsplit(" ", 1)[0],
])
def test_whitespace_bom_and_comment_do_not_change_it(decorate):
    assert md5_fingerprint(decorate(ED)) == ED_MD5


def test_ecdsa_keys_are_accepted():
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    line = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    assert len(md5_fingerprint(line).split(":")) == 16


@pytest.mark.parametrize("bad", ["", "ssh-ed25519", "ssh-ed25519 AAAA", ED.replace("ssh-ed25519", "ssh-rsa", 1)])
def test_malformed_lines_raise_a_chinese_error(bad):
    with pytest.raises(CnsrError, match="公钥"):
        md5_fingerprint(bad)
