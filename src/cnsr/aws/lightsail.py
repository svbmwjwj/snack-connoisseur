"""AWS Lightsail in one region: key pairs, an instance, its static IP and its firewall.

``cnsr up --aws`` creates one instance per node. The instance is named after the alias, or
``<alias>-1``, ``<alias>-2`` and so on when another resource of the region (a key pair named
after the alias, say) has the name; the record keeps the name used. Its static IP is
``<alias>-ip`` and it carries the tag ``cnsr=<alias>``; the tag is how a rerun tells its own
instance from a stranger's. Key pairs live per region and are matched by
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

from cnsr.aws import bundles
from cnsr.aws.errors import NOT_FOUND, LightsailError, name_taken, not_attached, reason_for, sdk_reason
from cnsr.aws.fingerprint import clean_pub_line, lightsail_fingerprint
from cnsr.aws.static_ips import StaticIps
from cnsr.errors import CnsrError
from cnsr.sizes import Offer, parse, pick
from cnsr.waiting import Deadline

logger = logging.getLogger(__name__)

# Every region Lightsail offers, by cnsr's code (country, then state or city when it is known).
REGIONS = {
    "jp-tokyo": "ap-northeast-1", "sg": "ap-southeast-1", "us-oregon": "us-west-2",
    "kr-seoul": "ap-northeast-2", "hk": "ap-east-1", "id-jakarta": "ap-southeast-3", "my": "ap-southeast-5",
    "in-mumbai": "ap-south-1", "au-sydney": "ap-southeast-2",
    "us-virginia": "us-east-1", "us-ohio": "us-east-2", "ca-montreal": "ca-central-1", "br-sao-paulo": "sa-east-1",
    "de-frankfurt": "eu-central-1", "ie-dublin": "eu-west-1", "uk-london": "eu-west-2", "fr-paris": "eu-west-3",
    "se-stockholm": "eu-north-1", "es": "eu-south-2",
}  # fmt: skip
REGION_NAMES = {
    "jp-tokyo": "日本东京", "sg": "新加坡", "us-oregon": "美国俄勒冈州",
    "kr-seoul": "韩国首尔", "hk": "中国香港", "id-jakarta": "印度尼西亚雅加达", "my": "马来西亚",
    "in-mumbai": "印度孟买", "au-sydney": "澳大利亚悉尼",
    "us-virginia": "美国弗吉尼亚州", "us-ohio": "美国俄亥俄州", "ca-montreal": "加拿大蒙特利尔", "br-sao-paulo": "巴西圣保罗",
    "de-frankfurt": "德国法兰克福", "ie-dublin": "爱尔兰都柏林", "uk-london": "英国伦敦", "fr-paris": "法国巴黎",
    "se-stockholm": "瑞典斯德哥尔摩", "es": "西班牙",
}  # fmt: skip
# The regions the menu shows first; the others are under "more regions".
FEATURED_REGIONS = ("jp-tokyo", "sg", "us-oregon")
# Regions AWS disables by default: an account must enable them before Lightsail works there.
OPT_IN = {"hk", "id-jakarta", "my", "es"}
BLUEPRINTS = ("debian_13", "debian_12")
LOGIN_USER = "admin"

TAG_KEY = "cnsr"
DEFAULT_KEY_PAIR = "LightsailDefaultKeyPair"
POLL_SECONDS = 5
# How many names a new instance tries: the alias, then <alias>-1 and so on.
NAME_TRIES = 10

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

    # --- sizes

    def offers(self) -> list[Offer]:
        """Every bundle a node can be in this region."""
        return bundles.offers(self._pages("get_bundles", "bundles", includeInactive=False))

    def pick(self, size: str) -> Offer:
        """The cheapest bundle of size ``size`` in this region."""
        offer = pick(self.offers(), size)
        if offer is None:
            cores, memory = parse(size)
            raise CnsrError(
                f"{self.region} 区域没有 {size} 规格的 Lightsail 套餐（至少 {cores} 核 {memory:g} GB），请换一个规格。"
            )
        return offer

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

    def ensure_instance(
        self, name: str, key_pair: str, size: str, alias: str, record: Callable[[str], None], *, numbered: bool = False
    ) -> tuple[str, bool]:
        """Create the dual-stack instance ``name`` for the node ``alias``, as the cheapest bundle of
        ``size``, or accept the one cnsr created for it earlier (tagged ``cnsr=<alias>``).

        With ``numbered`` (a new node), a name another resource of the region holds, an instance
        or a key pair or anything else, gives way to ``<name>-1``, ``<name>-2`` and so on: names
        are unique across a region's Lightsail resources. ``record(name)`` is called before each
        create, so that a run killed outright right after it leaves ``rm`` the name. Returns the
        instance's name and whether this call created it.
        """
        made: dict[str, str] = {}
        for number in range(NAME_TRIES if numbered else 1):
            candidate = f"{name}-{number}" if number else name
            found = self._call_if_exists("get_instance", instanceName=candidate)
            if found is not None:
                if _has_tag(found, alias):
                    record(candidate)
                    return candidate, False
                if numbered:
                    continue
                raise CnsrError(
                    f"{self.region} 区域已有同名实例 {candidate}，但它不是 cnsr 创建的（没有标签 {TAG_KEY}={alias}）。"
                    "为免误动，已停止；请换一个别名，或先自行处理这台实例。"
                )
            if not made:  # read once, for the first name that is free of instances
                made = {"zone": self._availability_zone(), "blueprint": self.pick_blueprint(), "bundle": self.pick(size).name}
            record(candidate)
            try:
                self._call(
                    "create_instances",
                    instanceNames=[candidate],
                    availabilityZone=made["zone"],
                    blueprintId=made["blueprint"],
                    bundleId=made["bundle"],
                    keyPairName=key_pair,
                    ipAddressType="dualstack",
                    tags=[{"key": TAG_KEY, "value": alias}],
                )
            except LightsailError as exc:
                if not name_taken(exc):
                    raise
                if not numbered:
                    raise CnsrError(
                        f"{self.region} 区域里已有名为 {candidate} 的其他 Lightsail 资源（例如 key pair），实例不能与它同名。"
                    ) from None
                continue
            return candidate, True
        raise CnsrError(
            f"{self.region} 区域里 {name} 到 {name}-{NAME_TRIES - 1} 都已被其他 Lightsail 资源占用，请换一个别名。"
        )

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
