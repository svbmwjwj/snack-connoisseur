"""Hetzner Primary IPs: the node's IPv4, made before its server and swapped while it is off.

``PrimaryIps`` is part of ``cnsr.hetzner.cloud.Hetzner``, whose ``_client``, ``location``,
``_server`` and ``_require_server`` it uses. A Primary IP is bound to its location; it can
only be assigned to or unassigned from a server that is off, and must be unassigned before
it is deleted.
"""

from __future__ import annotations

from typing import Any

from cnsr.errors import CnsrError
from cnsr.hetzner.api import HetznerError
from cnsr.hetzner.labels import TAG_KEY, labelled, labels


class PrimaryIps:
    """The Primary IP calls of ``Hetzner``."""

    def ensure_primary_ip(self, name: str, alias: str) -> str:
        """Make the unassigned IPv4 Primary IP ``name`` of node ``alias`` exist here, or accept
        the one cnsr created earlier; return its address.

        It is deleted with the server it gets assigned to.
        """
        found = self._primary_ip(name)
        if found is None:
            body = {
                "name": name,
                "type": "ipv4",
                "location": self.location,
                "auto_delete": True,
                "labels": labels(alias),
            }
            found = self._created(self._client.request("POST", "/primary_ips", json=body))
        elif not labelled(found, alias):
            raise CnsrError(
                f"Hetzner 项目里已有同名 Primary IP {name}，但它不是 cnsr 创建的（没有标签 {TAG_KEY}={alias}）。"
                "为免误动，已停止；请换一个别名，或先自行处理这个 Primary IP。"
            )
        elif (found.get("location") or {}).get("name") != self.location:
            raise CnsrError(
                f"Primary IP {name} 在位置 {(found.get('location') or {}).get('name')}，不在 {self.location}，"
                "无法给这个区域的服务器使用；请换一个别名，或先运行 cnsr rm 删除旧节点。"
            )
        return _address(found, name)

    def unassign_primary_ip(self, name: str) -> None:
        """Unassign Primary IP ``name`` from its server, which must be off; a free one counts as done."""
        found = self._require_primary_ip(name)
        if found.get("assignee_id") is None:
            return
        self._client.wait_action(self._client.request("POST", f"/primary_ips/{found['id']}/actions/unassign")["action"])

    def create_assigned_primary_ip(self, name: str, alias: str, server: str) -> str:
        """Create the IPv4 Primary IP ``name`` of node ``alias`` assigned to ``server``, which must
        be off and have no IPv4; return its address.

        Hetzner picks a new address for it, never the one of an IP that still exists.
        """
        server_id = self._require_server(server)["id"]
        body = {
            "name": name,
            "type": "ipv4",
            "assignee_type": "server",
            "assignee_id": server_id,
            "auto_delete": True,
            "labels": labels(alias),
        }
        return _address(self._created(self._client.request("POST", "/primary_ips", json=body)), name)

    def rename_primary_ip(self, name: str, new_name: str) -> None:
        """Rename Primary IP ``name`` to ``new_name``."""
        found = self._require_primary_ip(name)
        self._client.request("PUT", f"/primary_ips/{found['id']}", json={"name": new_name})

    def delete_primary_ip(self, name: str, alias: str) -> list[str]:
        """Delete Primary IP ``name`` when it is node ``alias``'s and free; return the Chinese errors.

        One that is already gone counts as done. One without the label, or still assigned to
        a server, is left alone and reported.
        """
        try:
            found = self._primary_ip(name)
            if found is None:
                return []
            if not labelled(found, alias):
                return [f"Primary IP {name} 不是 cnsr 创建的（没有标签 {TAG_KEY}={alias}），未删除"]
            if found.get("assignee_id") is not None:
                return [f"Primary IP {name} 还分配在一台服务器上，未删除"]
            self._client.request("DELETE", f"/primary_ips/{found['id']}")
        except HetznerError as exc:
            return [] if exc.code == "not_found" else [str(exc)]
        return []

    # --- helpers

    def _primary_ip(self, name: str) -> dict[str, Any] | None:
        found = self._client.request("GET", "/primary_ips", params={"name": name}).get("primary_ips") or []
        return found[0] if found else None

    def _require_primary_ip(self, name: str) -> dict[str, Any]:
        found = self._primary_ip(name)
        if found is None:
            raise CnsrError(f"Hetzner 项目里找不到 Primary IP {name}，可能已被删除。")
        return found

    def _created(self, answer: dict[str, Any]) -> dict[str, Any]:
        """The Primary IP of a create call, once the action that comes with it has finished."""
        if answer.get("action"):
            self._client.wait_action(answer["action"])
        return answer["primary_ip"]


def _address(found: dict[str, Any], name: str) -> str:
    address = found.get("ip")
    if not address:
        raise CnsrError(f"Hetzner 没有返回 Primary IP {name} 的地址，请稍后重试。")
    return address
