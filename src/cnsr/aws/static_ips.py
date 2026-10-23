"""Lightsail static IPs: allocate and attach, find by address, detach and release.

``StaticIps`` is part of ``cnsr.aws.lightsail.Lightsail``, whose ``_call``, ``_call_if_exists``,
``_pages``, ``_removal_error``, ``_clock`` and ``_sleep`` it uses.
"""

from __future__ import annotations

from typing import Any

from cnsr.aws.errors import LightsailError, not_attached
from cnsr.errors import CnsrError
from cnsr.waiting import Deadline

# Lightsail can show a static IP as attached for a moment after it was detached.
DETACH_POLL_SECONDS = 2
DETACH_TIMEOUT_SECONDS = 60


class StaticIps:
    """The static IP calls of ``Lightsail``."""

    def ensure_static_ip(self, instance: str, ip_name: str) -> str:
        """Make ``ip_name`` exist and be attached to ``instance``; return its IPv4 address."""
        found = self._call_if_exists("get_static_ip", staticIpName=ip_name)
        if found is None:
            self._call("allocate_static_ip", staticIpName=ip_name)
        else:
            info = found.get("staticIp", {})
            if info.get("isAttached"):
                if info.get("attachedTo") != instance:
                    raise CnsrError(
                        f"静态 IP {ip_name} 已经绑在实例 {info.get('attachedTo') or '其他实例'} 上，不是 {instance}，已停止。"
                    )
                return _address(info, ip_name)
        self._call("attach_static_ip", staticIpName=ip_name, instanceName=instance)
        info = self._call("get_static_ip", staticIpName=ip_name).get("staticIp", {})
        return _address(info, ip_name)

    def find_static_ip(self, address: str) -> tuple[str, str | None] | None:
        """The static IP with this address: its name and the instance it is attached to (None
        when free), or None when the region has no static IP with this address."""
        for info in self._pages("get_static_ips", "staticIps"):
            if info.get("ipAddress") != address:
                continue
            if not info.get("isAttached"):
                return info["name"], None
            if not info.get("attachedTo"):
                raise CnsrError(
                    f"静态 IP {info['name']} 绑在一台 AWS 没有给出名字的实例上，cnsr 无法确认改绑哪一台。"
                    "请先在 Lightsail 控制台把它解绑，再重跑。"
                )
            return info["name"], info["attachedTo"]
        return None

    def detach_static_ip(self, ip_name: str) -> None:
        """Detach the static IP from its instance and wait until Lightsail shows it free.

        One that is not attached counts as done.
        """
        try:
            self._call("detach_static_ip", staticIpName=ip_name)
        except LightsailError as exc:
            if not not_attached(exc):
                raise
        deadline = Deadline(DETACH_TIMEOUT_SECONDS, self._clock)
        while self._call("get_static_ip", staticIpName=ip_name).get("staticIp", {}).get("isAttached"):
            if deadline.passed():
                raise CnsrError(f"静态 IP {ip_name} 解绑 {DETACH_TIMEOUT_SECONDS} 秒后仍显示绑在实例上，请稍后重跑。")
            self._sleep(deadline.step(DETACH_POLL_SECONDS))

    def static_ip_attachment(self, ip_name: str) -> tuple[bool, str | None]:
        """Whether the static IP ``ip_name`` exists, and the instance it is attached to (None when free).

        An attached IP whose instance AWS does not name still answers with a (placeholder) name.
        """
        found = self._call_if_exists("get_static_ip", staticIpName=ip_name)
        if found is None:
            return False, None
        info = found.get("staticIp", {})
        if not info.get("isAttached"):
            return True, None
        return True, info.get("attachedTo") or "未知实例"

    def release_static_ip(self, ip_name: str) -> list[str]:
        """Detach and release the static IP; return the Chinese errors.

        What is already gone, or not attached, counts as done. Callers decide first whether
        the IP is theirs to release: a name alone proves nothing, and IPs cannot be tagged.
        """
        errors = [
            self._removal_error("detach_static_ip", staticIpName=ip_name),
            self._removal_error("release_static_ip", staticIpName=ip_name),
        ]
        return [error for error in errors if error]


def _address(info: dict[str, Any], ip_name: str) -> str:
    address = info.get("ipAddress")
    if not address:
        raise CnsrError(f"Lightsail 没有返回静态 IP {ip_name} 的地址，请稍后重试。")
    return address
