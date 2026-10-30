"""Hetzner Cloud in one location: SSH keys, a server, its Primary IP and its firewall.

``cnsr up --hetzner`` creates one server per node, named after the alias. Its IPv4 is a
Primary IP ``<alias>-ip`` made before the server, its firewall is ``<alias>-fw``, and all
three carry the label ``cnsr=<alias>``: the label is how a rerun or ``cnsr rm`` tells its own
resources from a stranger's of the same name. SSH keys are matched by fingerprint, never by
name, so a key the user already uploaded is reused whatever it is called.

Every API failure surfaces as ``HetznerError`` (``cnsr.hetzner.api``). The Primary IP calls
are in ``cnsr.hetzner.primary_ips``, the firewall calls in ``cnsr.hetzner.firewalls`` and the
server types on sale in ``cnsr.hetzner.sizes``.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable
from typing import Any

from cnsr.aws.fingerprint import clean_pub_line, md5_fingerprint
from cnsr.errors import CnsrError
from cnsr.hetzner import sizes
from cnsr.hetzner.api import HetznerClient, HetznerError
from cnsr.hetzner.firewalls import Firewalls
from cnsr.hetzner.labels import TAG_KEY, labelled, labels
from cnsr.hetzner.primary_ips import PrimaryIps
from cnsr.sizes import Offer, describe, parse, pick
from cnsr.waiting import Deadline

REGIONS = {
    "sg": "sin",
    "us-oregon": "hil",
    "us-virginia": "ash",
    "de-falkenstein": "fsn1",
    "de-nuremberg": "nbg1",
    "fi-helsinki": "hel1",
}
REGION_NAMES = {
    "sg": "新加坡",
    "us-oregon": "美国俄勒冈州",
    "us-virginia": "美国弗吉尼亚州",
    "de-falkenstein": "德国法尔肯施泰因",
    "de-nuremberg": "德国纽伦堡",
    "fi-helsinki": "芬兰赫尔辛基",
}
IMAGES = ("debian-13", "debian-12")
POLL_SECONDS = 2
OUT_OF_STOCK = ("resource_unavailable", "placement_error")


def stranger_server(name: str, alias: str | None = None) -> str:
    """Why a run stops at a server ``name`` that does not carry the label ``cnsr=<alias>``."""
    return (
        f"Hetzner 项目里已有同名服务器 {name}，但它不是 cnsr 创建的（没有标签 {TAG_KEY}={alias or name}）。"
        "为免误动，已停止；请换一个别名，或先自行处理这台服务器。"
    )


class KeyConflict(CnsrError):
    """An SSH key of the wanted name exists but holds a different key."""


class Hetzner(PrimaryIps, Firewalls):
    """The Hetzner resources of one cnsr node, in the location behind ``region_code``.

    ``client`` is the API client; its own ``sleep`` and ``clock`` wait for actions, these
    wait for a server to change state.
    """

    def __init__(
        self,
        region_code: str,
        client: HetznerClient,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if region_code not in REGIONS:
            raise CnsrError(f"Hetzner 没有区域 {region_code}，可选：{'、'.join(REGIONS)}。")
        self.region_code = region_code
        self.location = REGIONS[region_code]
        self._client = client
        self._sleep, self._clock = sleep, clock

    # --- sizes

    def offers(self) -> list[Offer]:
        """Every server type sold in this location now; empty when none is."""
        server_types = self._client.pages("/server_types", "server_types")
        pricing = self._client.request("GET", "/pricing")["pricing"]
        return sizes.offers(server_types, pricing, self.location)

    def catalog(self) -> dict[str, list[Offer]]:
        """Every x86 server type sold now, by region code, from one read of the types and prices.

        A location Hetzner gives no IPv4 price for sells nothing here: a server there needs one.
        """
        server_types = self._client.pages("/server_types", "server_types")
        pricing = self._client.request("GET", "/pricing")["pricing"]
        found = {}
        for code, location in REGIONS.items():
            try:
                found[code] = sizes.offers(server_types, pricing, location)
            except CnsrError:
                found[code] = []
        return found

    def pick(self, size: str) -> Offer:
        """The server type ``size`` (its name) sold here now.

        A size recorded before types were named (``1c1g``) is the cheapest shared type that meets it.
        """
        offers = self.offers()
        minimum = parse(size)
        if minimum is not None:
            offer = pick([offer for offer in offers if offer.plan != sizes.DEDICATED], size)
            if offer is None:
                raise CnsrError(
                    f"Hetzner {self._place()}暂时没有能买的 {size} 规格机型（至少 {minimum[0]} 核 {minimum[1]:g} GB），"
                    "请换一个规格或区域。"
                )
            return offer
        offer = next((offer for offer in offers if offer.name == size), None)
        if offer is None:
            raise CnsrError(f"Hetzner {self._place()}暂时买不到 {size}（无货，或这里不卖这个型号），请换一个型号或区域。")
        return offer

    # --- SSH keys

    def ensure_key_pair(self, name: str, pub_line: str) -> str:
        """Return the name of the SSH key holding ``pub_line``, uploading it as ``name`` if none does."""
        wanted = md5_fingerprint(pub_line)
        keys = self._client.pages("/ssh_keys", "ssh_keys")
        matches = [key["name"] for key in keys if _normal(key.get("fingerprint")) == wanted]
        if name in matches:
            return name
        if matches:
            return matches[0]
        if any(key.get("name") == name for key in keys):
            raise KeyConflict(
                f"Hetzner 项目里已有同名 SSH key {name}，但它的指纹和你选的 SSH 公钥不同。"
                "请换一把钥匙，或先在 Hetzner 控制台删除或改名这个 SSH key。"
            )
        self._client.request("POST", "/ssh_keys", json={"name": name, "public_key": clean_pub_line(pub_line)})
        return name

    # --- server

    def pick_image(self) -> str:
        """Return the first of ``IMAGES`` that Hetzner offers for x86."""
        offered = {
            image.get("name")
            for image in self._client.pages("/images", "images", {"type": "system", "architecture": "x86"})
            if image.get("status") == "available"
        }
        for name in IMAGES:
            if name in offered:
                return name
        raise CnsrError("Hetzner 没有可用的 Debian 13 或 Debian 12 系统镜像。")

    def ensure_instance(self, name: str, key: str, offer: Offer, primary_ip: str, alias: str | None = None) -> bool:
        """Create the server ``name`` of ``offer``'s type on the Primary IP ``primary_ip``, with an
        IPv6 of its own and the label ``cnsr=<alias>`` (``alias`` is ``name`` unless given), or
        accept the one cnsr created for it earlier.

        Returns True when the server was created by this call. When Hetzner has no stock the
        error lists the tier's other types sold here; none is bought instead.
        """
        alias = alias or name
        found = self._server(name)
        if found is not None:
            if labelled(found, alias):
                return False
            raise CnsrError(stranger_server(name, alias))
        ip = self._require_primary_ip(primary_ip)
        body = {
            "name": name,
            "location": self.location,
            "server_type": offer.name,
            "image": self.pick_image(),
            "ssh_keys": [key],
            "labels": labels(alias),
            "public_net": {"enable_ipv4": True, "ipv4": ip["id"], "enable_ipv6": True},
            "start_after_create": True,
        }
        try:
            created = self._client.request("POST", "/servers", json=body)
            # start_server comes as a next action: until it is done the server may still be off
            for action in [created["action"], *(created.get("next_actions") or [])]:
                self._client.wait_action(action)
        except HetznerError as exc:
            if exc.code in OUT_OF_STOCK:
                raise CnsrError(self._out_of_stock(offer, exc)) from exc
            raise
        return True

    def instance_tagged(self, name: str, alias: str) -> bool | None:
        """Whether server ``name`` carries the label ``cnsr=<alias>``; None when there is no such server."""
        found = self._server(name)
        return None if found is None else labelled(found, alias)

    def wait_running(self, name: str, timeout: int = 300) -> None:
        """Poll every ``POLL_SECONDS`` until the server runs; a state other than on its way fails at once."""
        self._wait_status(name, "running", ("initializing", "starting"), timeout)

    def power_off(self, name: str, timeout: int = 300) -> None:
        """Cut the server's power, as pulling the plug, and wait until it is off."""
        server = self._require_server(name)
        if server.get("status") != "off":
            self._client.wait_action(self._client.request("POST", f"/servers/{server['id']}/actions/poweroff")["action"])
        self._wait_status(name, "off", ("running", "stopping"), timeout)

    def power_on(self, name: str, timeout: int = 300) -> None:
        """Start the server and wait until it runs."""
        server = self._require_server(name)
        if server.get("status") != "running":
            self._client.wait_action(self._client.request("POST", f"/servers/{server['id']}/actions/poweron")["action"])
        self.wait_running(name, timeout)

    def instance_ipv6(self, name: str) -> str | None:
        """Return the server's IPv6 address, the first of its /64; None when it has none."""
        network = ((self._require_server(name).get("public_net") or {}).get("ipv6") or {}).get("ip")
        return str(ipaddress.IPv6Network(network, strict=False)[1]) if network else None

    def traffic(self, name: str) -> tuple[int, int] | None:
        """The server's outgoing traffic this billing period and its included traffic, in bytes;
        None when there is no such server."""
        found = self._server(name)
        if found is None:
            return None
        return int(found.get("outgoing_traffic") or 0), int(found.get("included_traffic") or 0)

    # --- removal

    def delete_instance(self, name: str) -> list[str]:
        """Delete the server and wait until it is gone; return the Chinese error, if any.

        One that is already gone counts as done. Its Primary IP is unassigned (and deleted
        with it when ``auto_delete`` is set), its firewall detached.
        """
        try:
            found = self._server(name)
            if found is None:
                return []
            answer = self._client.request("DELETE", f"/servers/{found['id']}")
            if answer and answer.get("action"):
                self._client.wait_action(answer["action"])
        except HetznerError as exc:
            return [] if exc.code == "not_found" else [str(exc)]
        except CnsrError as exc:
            return [str(exc)]
        return []

    # --- helpers

    def _server(self, name: str) -> dict[str, Any] | None:
        found = self._client.request("GET", "/servers", params={"name": name}).get("servers") or []
        return found[0] if found else None

    def _require_server(self, name: str) -> dict[str, Any]:
        found = self._server(name)
        if found is None:
            raise CnsrError(f"Hetzner 项目里找不到服务器 {name}，可能已被删除。")
        return found

    def _wait_status(self, name: str, wanted: str, passing: tuple[str, ...], timeout: int) -> None:
        deadline = Deadline(timeout, self._clock)
        words = {"running": "运行中", "off": "关机"}[wanted]
        while True:
            status = self._require_server(name).get("status") or "未知"
            if status == wanted:
                return
            if status not in passing:
                raise CnsrError(f"服务器 {name} 的状态是 {status}，不是{words}，无法继续。")
            if deadline.passed():
                raise CnsrError(f"等了 {timeout} 秒，服务器 {name} 还没有进入{words}状态，请稍后在 Hetzner 控制台查看。")
            self._sleep(deadline.step(POLL_SECONDS))

    def _out_of_stock(self, offer: Offer, exc: HetznerError) -> str:
        text = f"Hetzner {self._place()}创建 {offer.name} 失败：{_stock_reason(exc)}"
        try:
            others = [
                other
                for other in sorted(self.offers(), key=lambda other: (other.monthly, other.name))
                if other.name != offer.name and other.plan == offer.plan
                and other.cores >= offer.cores and other.memory >= offer.memory
            ]
        except CnsrError:
            others = []
        if not others:
            return f"{text}。同一方案里这个区域暂时没有别的能买的型号，请稍后重跑，或换一个型号或区域。"
        listed = "\n".join(f"  {describe(other)}" for other in others)
        return (
            f"{text}。同一方案里这个区域还能买的、配置不低于 {offer.name} 的型号：\n{listed}\n"
            "cnsr 不会自动换型号；请稍后重跑，或换一个型号或区域。"
        )

    def _place(self) -> str:
        return f"{self.region_code}（{REGION_NAMES[self.region_code]}）"


def _stock_reason(exc: HetznerError) -> str:
    return "这个型号暂时无货或不能购买" if exc.code == "resource_unavailable" else "暂时无法在这个位置放置服务器（多为无货）"


def _normal(fingerprint: Any) -> str:
    return str(fingerprint or "").strip().casefold()
