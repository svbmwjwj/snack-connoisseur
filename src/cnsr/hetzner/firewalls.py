"""Hetzner cloud firewalls: one per node, applied to its server only, inbound rules only.

``Firewalls`` is part of ``cnsr.hetzner.cloud.Hetzner``, whose ``_client`` and
``_require_server`` it uses. With a firewall applied, inbound traffic passes only where a rule
allows it; with no outbound rules, all outbound traffic passes. Rule changes and applying a
firewall are actions: they take effect once those finish, which every call here waits for.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cnsr.errors import CnsrError
from cnsr.hetzner.api import HetznerError
from cnsr.hetzner.labels import TAG_KEY, labelled, labels, selector

_OPEN_TO_ALL = ["0.0.0.0/0", "::/0"]


class Firewalls:
    """The firewall calls of ``Hetzner``."""

    def ensure_firewall(self, name: str, alias: str) -> None:
        """Make the firewall ``name`` of node ``alias`` exist, or accept the one cnsr created earlier.

        A new one has no rules until ``set_rules``.
        """
        found = self._firewall(name)
        if found is None:
            answer = self._client.request("POST", "/firewalls", json={"name": name, "labels": labels(alias), "rules": []})
            self._wait_all(answer)
        elif not labelled(found, alias):
            raise CnsrError(
                f"Hetzner 项目里已有同名防火墙 {name}，但它不是 cnsr 创建的（没有标签 {TAG_KEY}={alias}）。"
                "为免误动，已停止；请换一个别名，或先自行处理这个防火墙。"
            )

    def attach_firewall(self, name: str, server: str) -> None:
        """Apply the firewall ``name`` to ``server``; one already applied counts as done."""
        found = self._require_firewall(name)
        server_id = self._require_server(server)["id"]
        if server_id in _applied_to(found):
            return
        body = {"apply_to": [{"type": "server", "server": {"id": server_id}}]}
        try:
            answer = self._client.request("POST", f"/firewalls/{found['id']}/actions/apply_to_resources", json=body)
        except HetznerError as exc:
            if exc.code == "firewall_already_applied":
                return
            raise
        self._wait_all(answer)

    def set_rules(self, name: str, ssh_ports: Iterable[int], app_ports: tuple[tuple[int, str], ...]) -> None:
        """Allow exactly the TCP ``ssh_ports`` and the ``(port, protocol)`` pairs in ``app_ports`` in,
        from any IPv4 and IPv6 address, replacing the rules before; anything else inbound is dropped."""
        found = self._require_firewall(name)
        wanted = dict.fromkeys([*((port, "tcp") for port in ssh_ports), *app_ports])
        rules = [
            {"direction": "in", "protocol": protocol, "port": str(port), "source_ips": list(_OPEN_TO_ALL)}
            for port, protocol in wanted
        ]
        self._wait_all(self._client.request("POST", f"/firewalls/{found['id']}/actions/set_rules", json={"rules": rules}))

    def delete_firewall(self, alias: str) -> list[str]:
        """Delete the firewalls labelled ``cnsr=<alias>``; return the Chinese errors.

        Run after the server is deleted. One still applied to a resource is refused by Hetzner
        and reported, never forced.
        """
        errors: list[str] = []
        try:
            found = self._client.pages("/firewalls", "firewalls", {"label_selector": selector(alias)})
        except HetznerError as exc:
            return [str(exc)]
        for firewall in found:
            try:
                self._client.request("DELETE", f"/firewalls/{firewall['id']}")
            except HetznerError as exc:
                if exc.code == "not_found":
                    continue
                if exc.code == "resource_in_use":
                    errors.append(f"防火墙 {firewall.get('name')} 还挂在其他资源上，Hetzner 拒绝删除，请到 Hetzner 控制台处理")
                else:
                    errors.append(str(exc))
        return errors

    # --- helpers

    def _firewall(self, name: str) -> dict[str, Any] | None:
        found = self._client.request("GET", "/firewalls", params={"name": name}).get("firewalls") or []
        return found[0] if found else None

    def _require_firewall(self, name: str) -> dict[str, Any]:
        found = self._firewall(name)
        if found is None:
            raise CnsrError(f"Hetzner 项目里找不到防火墙 {name}，可能已被删除。")
        return found

    def _wait_all(self, answer: dict[str, Any] | None) -> None:
        for action in (answer or {}).get("actions") or []:
            self._client.wait_action(action)


def _applied_to(firewall: dict[str, Any]) -> set[int]:
    return {
        (entry.get("server") or {}).get("id")
        for entry in firewall.get("applied_to") or []
        if entry.get("type") == "server"
    }
