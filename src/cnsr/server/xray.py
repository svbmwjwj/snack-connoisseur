"""Xray on the node: its identity, the x25519 key pair and the REALITY server config.

The bundled ``config.json`` is a template with ``PLACEHOLDER_*`` values. ``render_config``
fills it in and ``read_identity`` reads the same five values back from a config already on the
node, so a rerun keeps the UUID, keys and SNI. ``compose.yml`` is uploaded as it is.
"""

from __future__ import annotations

import base64
import copy
import json
import random
import re
import uuid
from importlib import resources

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cnsr.errors import CnsrError

XRAY_IMAGE = "ghcr.io/xtls/xray-core:26.3.27"
NODE_DIR = "/opt/cnsr"
COMPOSE_PATH = f"{NODE_DIR}/xray/compose.yml"
CONFIG_PATH = f"{NODE_DIR}/xray/conf/config.json"
BACKUPS_PATH = f"{NODE_DIR}/backups.json"
NODE_ENV_PATH = f"{NODE_DIR}/node.env"
# verify-reality.sh reads the client's values from here (root only) and deletes the file.
VERIFY_ENV_PATH = f"{NODE_DIR}/verify.env"
# The node's identity for its SNI rotation service (root only); never holds the private key.
IDENTITY_ENV_PATH = f"{NODE_DIR}/identity.env"

_ASSET_PACKAGE = "cnsr.assets.xray"
_DEST_PORT = ":443"
_NOT_JSON = "节点上已有的 Xray 配置不是有效的 JSON，无法沿用其中的身份信息。"
_INCOMPLETE = "节点上已有的 Xray 配置缺少 UUID、密钥、shortId、SNI 或回源地址，或格式不对。"
# 32 key bytes as xray writes them: base64url without padding.
_X25519_KEY = re.compile(r"[A-Za-z0-9_-]{43}")


def new_identity(rng: random.Random) -> tuple[str, str]:
    """A random version-4 UUID and a 16-digit hex shortId, both drawn from ``rng``.

    Pass ``secrets.SystemRandom()`` outside tests.
    """
    return str(uuid.UUID(int=rng.getrandbits(128), version=4)), f"{rng.getrandbits(64):016x}"


def parse_x25519(text: str) -> tuple[str, str]:
    """The private and public key from the output of ``xray x25519``.

    Reads both layouts: ``PrivateKey:`` with ``Password:``, and ``Private key:`` with
    ``Public key:``. Other lines are ignored.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        # "Password (PublicKey)" is the same field as "Password".
        fields.setdefault(re.sub(r"\(.*", "", name).replace(" ", "").lower(), value.strip())
    private = fields.get("privatekey", "")
    public = fields.get("publickey") or fields.get("password", "")
    if not private or not public:
        raise CnsrError("无法从 x25519 的输出里读到私钥和公钥。")
    return private, public


def derive_public_key(private_key: str) -> str:
    """The x25519 public key of ``private_key``, both in xray's format (base64url, no padding).

    Computed here rather than with ``xray x25519 -i`` on the node, so the private key never
    appears on a command line there.
    """
    if not _X25519_KEY.fullmatch(private_key):
        raise CnsrError("节点上 Xray 配置里的私钥格式不对，无法算出公钥。")
    raw = base64.urlsafe_b64decode(private_key + "=")
    public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")


def config_template() -> dict:
    """The bundled server config with its ``PLACEHOLDER_*`` values."""
    return json.loads(_asset("config.json"))


def compose_text() -> str:
    """The bundled ``compose.yml`` with LF line endings."""
    return _asset("compose.yml")


def render_config(
    template: dict, *, alias: str, uuid: str, private_key: str, short_id: str, sni: str, dest_ip: str
) -> str:
    """The server config for one node as JSON text; ``template`` is not changed.

    The inbound tag becomes ``alias`` and the client email ``<alias>@cnsr``. REALITY borrows the
    certificate of ``sni``, fetched from ``dest_ip``:443.
    """
    config = copy.deepcopy(template)
    inbound = config["inbounds"][0]
    inbound["tag"] = alias
    client = inbound["settings"]["clients"][0]
    client["id"] = uuid
    client["email"] = f"{alias}@cnsr"
    reality = inbound["streamSettings"]["realitySettings"]
    reality["privateKey"] = private_key
    reality["shortIds"] = [short_id]
    reality["serverNames"] = [sni]
    reality["dest"] = f"{dest_ip}{_DEST_PORT}"
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def verify_env(uuid: str, public_key: str, short_id: str, sni: str) -> str:
    """The text of ``VERIFY_ENV_PATH``: the values a REALITY client needs, one ``KEY=value`` per line."""
    return f"UUID={uuid}\nPUBLIC_KEY={public_key}\nSHORT_ID={short_id}\nSNI={sni}\n"


def identity_env(alias: str, uuid: str, public_key: str, short_id: str) -> str:
    """The text of ``IDENTITY_ENV_PATH``: what cnsr-rotate.sh needs for its URLs and handshake check."""
    return f"ALIAS={alias}\nUUID={uuid}\nPUBLIC_KEY={public_key}\nSHORT_ID={short_id}\n"


def read_identity(config_text: str) -> dict[str, str]:
    """The five values ``render_config`` set, read back from a config on the node.

    Returns ``uuid``, ``private_key``, ``short_id``, ``sni`` and ``dest_ip`` (without ``:443``).
    A config that does not have them is an error.
    """
    try:
        config = json.loads(config_text.removeprefix("\ufeff"))
    except ValueError:
        raise CnsrError(_NOT_JSON) from None
    try:
        inbound = config["inbounds"][0]
        reality = inbound["streamSettings"]["realitySettings"]
        found = (
            inbound["settings"]["clients"][0]["id"],
            reality["privateKey"],
            reality["shortIds"][0],
            reality["serverNames"][0],
            reality["dest"],
        )
    except (KeyError, IndexError, TypeError):
        raise CnsrError(_INCOMPLETE) from None
    uid, private_key, short_id, sni, dest = found
    if not all(isinstance(value, str) and value for value in found):
        raise CnsrError(_INCOMPLETE)
    dest_ip = dest.removesuffix(_DEST_PORT)
    if dest_ip == dest or not dest_ip:  # the address must be followed by :443
        raise CnsrError(_INCOMPLETE)
    return {"uuid": uid, "private_key": private_key, "short_id": short_id, "sni": sni, "dest_ip": dest_ip}


def _asset(name: str) -> str:
    text = (resources.files(_ASSET_PACKAGE) / name).read_text(encoding="utf-8-sig")
    return text.replace("\r\n", "\n")
