"""Fingerprints the way Lightsail and Hetzner report them for imported keys.

Lightsail reports ED25519 keys as the base64 of the SHA-256 of the SSH wire-format key blob,
with the ``=`` padding, and RSA keys as the MD5 of the PKCS#1 DER encoding of the public key,
as colon-separated lowercase hex; other key types cannot be imported. Hetzner reports every
key as the MD5 of the key blob, as colon-separated lowercase hex (``ssh-keygen -E md5``).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from cnsr.errors import CnsrError

_MALFORMED = "SSH 公钥格式不对，应该是一行“类型 base64 内容 [备注]”。"


def clean_pub_line(pub_line: str) -> str:
    """Return the key line without a BOM or surrounding whitespace; it must be a single line."""
    text = pub_line.lstrip("\ufeff").strip()
    if not text or "\n" in text or "\r" in text:
        raise CnsrError(_MALFORMED)
    return text


def lightsail_fingerprint(pub_line: str) -> str:
    """Return the fingerprint Lightsail shows for ``pub_line``, an OpenSSH public key line."""
    key_type, data = _fields(pub_line)
    if key_type not in ("ssh-ed25519", "ssh-rsa"):
        raise CnsrError(f"Lightsail 只支持 ED25519 和 RSA 公钥，这把钥匙的类型是 {key_type}。")
    key = _load(key_type, data)
    if key_type == "ssh-ed25519":
        return base64.b64encode(hashlib.sha256(base64.b64decode(data)).digest()).decode("ascii")
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    return _colon_hex(hashlib.md5(der, usedforsecurity=False).digest())


def md5_fingerprint(pub_line: str) -> str:
    """Return the MD5 fingerprint of ``pub_line``'s key blob, the one Hetzner shows for SSH keys."""
    key_type, data = _fields(pub_line)
    _load(key_type, data)
    return _colon_hex(hashlib.md5(base64.b64decode(data), usedforsecurity=False).digest())


def _fields(pub_line: str) -> tuple[str, str]:
    fields = clean_pub_line(pub_line).split()
    if len(fields) < 2:
        raise CnsrError(_MALFORMED)
    return fields[0], fields[1]


def _load(key_type: str, data: str) -> Any:
    try:
        # Parsing also checks that the key type inside the blob agrees with the line's type.
        return serialization.load_ssh_public_key(f"{key_type} {data}".encode("ascii"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise CnsrError(_MALFORMED) from exc


def _colon_hex(digest: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in digest)
