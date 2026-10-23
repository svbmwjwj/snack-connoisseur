"""Fingerprints the way Lightsail reports them for imported key pairs.

ED25519 keys are reported as the base64 of the SHA-256 of the SSH wire-format key blob,
with the ``=`` padding. RSA keys are reported as the MD5 of the PKCS#1 DER encoding of the
public key, as colon-separated lowercase hex. Other key types cannot be imported.
"""

from __future__ import annotations

import base64
import hashlib

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
    fields = clean_pub_line(pub_line).split()
    if len(fields) < 2:
        raise CnsrError(_MALFORMED)
    key_type, data = fields[0], fields[1]
    if key_type not in ("ssh-ed25519", "ssh-rsa"):
        raise CnsrError(f"Lightsail 只支持 ED25519 和 RSA 公钥，这把钥匙的类型是 {key_type}。")
    try:
        # Parsing also checks that the key type inside the blob agrees with the line's type.
        key = serialization.load_ssh_public_key(f"{key_type} {data}".encode("ascii"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise CnsrError(_MALFORMED) from exc
    if key_type == "ssh-ed25519":
        return base64.b64encode(hashlib.sha256(base64.b64decode(data)).digest()).decode("ascii")
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
    digest = hashlib.md5(der, usedforsecurity=False).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
