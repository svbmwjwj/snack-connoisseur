"""AWS Lightsail in one region: key pairs, an instance, its static IP and its firewall.

``cnsr add --aws`` creates one instance per node. The instance is named after the alias, its
static IP is ``<alias>-ip`` and it carries the tag ``cnsr=<alias>``; the tag is how a rerun
tells its own instance from a stranger's. Key pairs live per region and are matched by
fingerprint, never by name, so a key the user already imported is reused whatever it is called.

Every AWS failure surfaces as ``LightsailError`` (``cnsr.aws.errors``): a Chinese message that
names the operation and the region and never repeats the AWS text of credential or permission
errors. The static IP calls are in ``cnsr.aws.static_ips``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore import exceptions as boto_exc

from cnsr.aws.errors import NOT_FOUND, LightsailError, not_attached, reason_for, sdk_reason
from cnsr.aws.fingerprint import clean_pub_line, lightsail_fingerprint
from cnsr.aws.static_ips import StaticIps
from cnsr.errors import CnsrError
from cnsr.waiting import Deadline

logger = logging.getLogger(__name__)

REGIONS = {"jp": "ap-northeast-1", "sg": "ap-southeast-1", "us": "us-west-2"}
REGION_NAMES = {"jp": "东京", "sg": "新加坡", "us": "俄勒冈"}
BUNDLE = "nano_3_0"
BLUEPRINTS = ("debian_13", "debian_12")
LOGIN_USER = "admin"

TAG_KEY = "cnsr"
DEFAULT_KEY_PAIR = "LightsailDefaultKeyPair"
POLL_SECONDS = 5

_OPEN_TO_ALL = {"cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]}
_SSH_PORT = (22, "tcp")

class KeyConflict(CnsrError):
    """A key pair of the wanted name exists but holds a different key."""


class Lightsail(StaticIps):
    """The Lightsail resources of one cnsr node, in the region behind ``region_code``.

    ``client`` is a boto3 Lightsail client (tests pass a stubbed one); without it a client
    for the region is made from boto3's default credential chain. ``sleep`` is used while
    waiting for an instance to run and for a static IP to detach.
    """

    def __init__(
        self,
        region_code: str,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if region_code not in REGIONS:
            raise CnsrError(f"不支持的区域 {region_code}，可选：{'、'.join(REGIONS)}。")
        self.region = REGIONS[region_code]
        self._client = client if client is not None else self._make_client()
        self._sleep, self._clock = sleep, clock

    def _make_client(self) -> Any:
        try:
            return boto3.client("lightsail", region_name=self.region)
        except boto_exc.BotoCoreError as exc:
            raise LightsailError("create_client", self.region, "", "", sdk_reason(exc)) from exc

    # --- key pairs

    def ensure_key_pair(self, name: str, pub_line: str) -> str:
        """Return the name of the key pair holding ``pub_line``, importing it as ``name`` if none does."""
        if name == DEFAULT_KEY_PAIR:
            raise CnsrError(f"{DEFAULT_KEY_PAIR} 是 Lightsail 自带的默认钥匙，cnsr 不使用它，请换一个钥匙名称。")
        wanted = _normal(lightsail_fingerprint(pub_line))
        pairs = [
            pair
            for pair in self._pages("get_key_pairs", "keyPairs", includeDefaultKeyPair=False)
            if pair.get("name") != DEFAULT_KEY_PAIR
        ]
        matches = [pair["name"] for pair in pairs if _normal(pair.get("fingerprint")) == wanted]
        if name in matches:
            return name
        if matches:
            return matches[0]
        if any(pair.get("name") == name for pair in pairs):
            raise KeyConflict(
                f"{self.region} 区域已有同名 key pair {name}，但它的指纹和你选的 SSH 公钥不同。"
                "请换一把钥匙，或先在 Lightsail 控制台删除或改名这个 key pair。"
            )
        self._call("import_key_pair", keyPairName=name, publicKeyBase64=clean_pub_line(pub_line))
        return name

    # --- instance

    def pick_blueprint(self) -> str:
        """Return the first of ``BLUEPRINTS`` that this region offers."""
        offered = {
            blueprint.get("blueprintId")
            for blueprint in self._pages("get_blueprints", "blueprints")
            if blueprint.get("isActive", True)
        }
        for blueprint_id in BLUEPRINTS:
            if blueprint_id in offered:
                return blueprint_id
        raise CnsrError(f"{self.region} 区域没有可用的 Debian 13 或 Debian 12 系统镜像。")

    def ensure_instance(self, name: str, key_pair: str) -> bool:
        """Create the dual-stack instance ``name``, or accept the one cnsr created earlier.

        Returns True when the instance was created by this call.
        """
        found = self._call_if_exists("get_instance", instanceName=name)
        if found is not None:
            if _has_tag(found, name):
                return False
            raise CnsrError(
                f"{self.region} 区域已有同名实例 {name}，但它不是 cnsr 创建的（没有标签 {TAG_KEY}={name}）。"
                "为免误动，已停止；请换一个别名，或先自行处理这台实例。"
            )
        zone = self._availability_zone()
        blueprint = self.pick_blueprint()
        self._call(
            "create_instances",
            instanceNames=[name],
            availabilityZone=zone,
            blueprintId=blueprint,
            bundleId=BUNDLE,
            keyPairName=key_pair,
            ipAddressType="dualstack",
            tags=[{"key": TAG_KEY, "value": name}],
        )
        return True

    def instance_tagged(self, name: str, alias: str) -> bool | None:
        """Whether instance ``name`` carries the tag ``cnsr=<alias>``; None when there is no such instance."""
        found = self._call_if_exists("get_instance", instanceName=name)
        return None if found is None else _has_tag(found, alias)

    def wait_running(self, name: str, timeout: int = 300) -> None:
        """Poll every ``POLL_SECONDS`` until the instance runs; any state but none, pending or running fails at once.

        Lightsail reports ``none`` for the first seconds after ``create_instances``, before ``pending``.
        """
        deadline = Deadline(timeout, self._clock)
        while True:
            state = self._call("get_instance_state", instanceName=name).get("state", {}).get("name") or "未知"
            if state == "running":
                return
            if state not in ("none", "pending"):
                raise CnsrError(f"实例 {name} 的状态是 {state}，不是运行中，无法继续。")
            if deadline.passed():
                raise CnsrError(f"等了 {timeout} 秒，实例 {name} 还没有进入运行状态，请稍后在 Lightsail 控制台查看。")
            self._sleep(deadline.step(POLL_SECONDS))

    def instance_ipv6(self, name: str) -> str | None:
        """Return the instance's first IPv6 address, or None when it has none."""
        addresses = self._call("get_instance", instanceName=name).get("instance", {}).get("ipv6Addresses") or []
        return addresses[0] if addresses else None

    def set_firewall(self, name: str, app_ports: tuple[tuple[int, str], ...]) -> None:
        """Open exactly 22/tcp and the ``(port, protocol)`` pairs in ``app_ports`` to IPv4 and IPv6;
        anything else is closed."""
        port_infos = [
            {"fromPort": port, "toPort": port, "protocol": protocol, **{k: list(v) for k, v in _OPEN_TO_ALL.items()}}
            for port, protocol in (_SSH_PORT, *app_ports)
        ]
        self._call("put_instance_public_ports", instanceName=name, portInfos=port_infos)

    # --- removal

    def delete_instance(self, name: str) -> list[str]:
        """Delete the instance; return the Chinese error, if any. One that is already gone counts as done."""
        error = self._removal_error("delete_instance", instanceName=name)
        return [error] if error else []

    # --- helpers

    def _availability_zone(self) -> str:
        regions = self._call("get_regions", includeAvailabilityZones=True).get("regions") or []
        for region in regions:
            if region.get("name") != self.region:
                continue
            for zone in region.get("availabilityZones") or []:
                if zone.get("state") == "available" and zone.get("zoneName"):
                    return zone["zoneName"]
        return f"{self.region}a"

    def _pages(self, operation: str, key: str, **params: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = None
        while True:
            page = self._call(operation, **params, **({"pageToken": token} if token else {}))
            items.extend(page.get(key) or [])
            token = page.get("nextPageToken")
            if not token:
                return items

    def _removal_error(self, operation: str, **params: Any) -> str | None:
        """Run a delete-like call; None when it worked or there was nothing to do, else the error text."""
        try:
            self._call(operation, **params)
        except LightsailError as exc:
            if exc.code == NOT_FOUND or (operation == "detach_static_ip" and not_attached(exc)):
                return None
            return str(exc)
        return None

    def _call_if_exists(self, operation: str, **params: Any) -> dict[str, Any] | None:
        try:
            return self._call(operation, **params)
        except LightsailError as exc:
            if exc.code == NOT_FOUND:
                return None
            raise

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        began = time.monotonic()

        def log(result: str) -> None:
            logger.info("AWS Lightsail %s（%s）→ %s，耗时 %.1f 秒", operation, self.region, result, time.monotonic() - began)

        try:
            answer = getattr(self._client, operation)(**params)
        except boto_exc.ClientError as exc:
            error = exc.response.get("Error", {})
            code, message = str(error.get("Code", "")), str(error.get("Message", ""))
            log(f"失败 {code or '未知错误'}")
            raise LightsailError(operation, self.region, code, message, reason_for(code, message)) from exc
        except boto_exc.BotoCoreError as exc:
            log(f"失败 {type(exc).__name__}")
            raise LightsailError(operation, self.region, "", "", sdk_reason(exc)) from exc
        log("成功")
        return answer


def _has_tag(found: dict[str, Any], value: str) -> bool:
    """Whether a ``get_instance`` answer shows the tag ``cnsr=<value>``."""
    tags = found.get("instance", {}).get("tags") or []
    return any(tag.get("key") == TAG_KEY and tag.get("value") == value for tag in tags)


def _normal(fingerprint: Any) -> str:
    return str(fingerprint or "").strip().casefold()
