"""A fake Hetzner location for the ``cnsr up``, ``cnsr rm`` and ``cnsr list`` tests."""

from __future__ import annotations

from decimal import Decimal

from cnsr.errors import CnsrError
from cnsr.sizes import Offer, parse, pick

HZ_IP = "198.51.100.40"
HZ_IPV6 = "2001:db8:40::1"
TB = 1024**4


def offer(name: str, cores: float, memory: float, monthly: str, plan: str = "regular_purpose") -> Offer:
    return Offer(name, name, cores, memory, 40, 20 * TB, Decimal("1.19"), Decimal(monthly), Decimal("0.61"), "EUR", plan)


def default_stock() -> list[Offer]:
    return [
        offer("cpx02", 1, 1, "4.15"), offer("cpx22", 2, 4, "9.51"), offer("cpx32", 4, 8, "16.48"),
        offer("cx23", 2, 4, "4.49", "cost_optimized"), offer("ccx13", 2, 8, "14.00", "general_purpose"),
    ]  # fmt: skip


class FakeHetzner:
    """The Hetzner of one location: it records its calls and holds servers, Primary IPs and firewalls.

    ``servers``, ``ips`` and ``firewalls`` map a name to whether the resource carries cnsr's
    label; the ``ensure_*`` calls fill them in and refuse a stranger's resource like the real
    ones, so a test puts a stranger's in first. ``assigned`` maps a Primary IP to the server it
    is assigned to (None: free), ``attached`` a firewall to its server, ``rules`` a firewall to
    its ``(ssh_ports, app_ports)``, and ``off`` holds the servers powered off: ``wait_running``
    fails on one, as on Hetzner. Primary IPs are assigned or unassigned only while their server
    is off, as on Hetzner.

    ``stock`` is what ``offers`` lists; with ``out_of_stock`` set, ``ensure_instance`` raises
    it instead of creating a server. ``delete_*_errors`` are what the removal calls return (the
    resource is then left in place). ``addresses`` gives Primary IPs their own address;
    ``fresh_addresses`` is what each new one gets in turn (then ``HZ_IP``, or a new address
    when the IP is created assigned). ``used`` maps a server to its outgoing traffic.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.servers: dict[str, bool] = {}
        self.ips: dict[str, bool] = {}
        self.assigned: dict[str, str | None] = {}
        self.addresses: dict[str, str] = {}
        self.fresh_addresses: list[str] = []
        self.firewalls: dict[str, bool] = {}
        self.attached: dict[str, str] = {}
        self.rules: dict[str, tuple[tuple[int, ...], tuple[tuple[int, str], ...]]] = {}
        self.off: set[str] = set()
        self.stock: list[Offer] = default_stock()
        self.stock_at: dict[str, list[Offer]] = {}  # a region's own stock, where it differs
        self.out_of_stock: CnsrError | None = None
        self.used: dict[str, int] = {}
        self.delete_instance_errors: list[str] = []
        self.delete_ip_errors: list[str] = []
        self.delete_firewall_errors: list[str] = []

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    # --- sizes

    def offers(self) -> list[Offer]:
        self.calls.append(("offers",))
        return list(self.stock)

    def catalog(self) -> dict[str, list[Offer]]:
        from cnsr.hetzner.cloud import REGIONS

        self.calls.append(("catalog",))
        return {code: list(self.stock_at.get(code, self.stock)) for code in REGIONS}

    def pick(self, size: str) -> Offer:
        self.calls.append(("pick", size))
        if parse(size) is not None:  # a size recorded before types were named
            found = pick([offer for offer in self.stock if offer.plan != "general_purpose"], size)
        else:
            found = next((offer for offer in self.stock if offer.name == size), None)
        if found is None:
            raise CnsrError(f"Hetzner 暂时买不到 {size}。")
        return found

    # --- keys and servers

    def ensure_key_pair(self, name: str, pub_line: str) -> str:
        self.calls.append(("ensure_key_pair", name, pub_line))
        return name

    def pick_image(self) -> str:
        self.calls.append(("pick_image",))
        return "debian-13"

    def ensure_instance(self, name: str, key: str, offer: Offer, primary_ip: str) -> bool:
        self.calls.append(("ensure_instance", name, key, offer and offer.name, primary_ip))
        if self.servers.get(name) is False:
            raise CnsrError(f"Hetzner 项目里已有同名服务器 {name}，但它不是 cnsr 创建的。")
        if name in self.servers:
            return False
        if primary_ip not in self.ips:
            raise CnsrError(f"Hetzner 项目里找不到 Primary IP {primary_ip}。")
        if self.out_of_stock is not None:
            raise self.out_of_stock
        self.servers[name] = True
        self.assigned[primary_ip] = name
        return True

    def instance_tagged(self, name: str, alias: str) -> bool | None:
        self.calls.append(("instance_tagged", name, alias))
        return self.servers.get(name)

    def wait_running(self, name: str) -> None:
        self.calls.append(("wait_running", name))
        if name in self.off:
            raise CnsrError(f"服务器 {name} 的状态是 off，不是运行中，无法继续。")

    def power_off(self, name: str) -> None:
        self.calls.append(("power_off", name))
        self.off.add(name)

    def power_on(self, name: str) -> None:
        self.calls.append(("power_on", name))
        self.off.discard(name)

    def instance_ipv6(self, name: str) -> str | None:
        self.calls.append(("instance_ipv6", name))
        return HZ_IPV6

    def traffic(self, name: str) -> tuple[int, int] | None:
        self.calls.append(("traffic", name))
        return (self.used.get(name, 0), 20 * TB) if name in self.servers else None

    def delete_instance(self, name: str) -> list[str]:
        self.calls.append(("delete_instance", name))
        if self.delete_instance_errors:
            return list(self.delete_instance_errors)
        self.servers.pop(name, None)
        self.off.discard(name)
        for ip, server in list(self.assigned.items()):
            if server != name:
                continue
            if self.ips.get(ip):  # cnsr's IPs have auto_delete set
                self._drop_ip(ip)
            else:
                self.assigned[ip] = None
        self.attached = {fw: server for fw, server in self.attached.items() if server != name}
        return []

    # --- Primary IPs

    def ensure_primary_ip(self, name: str, alias: str) -> str:
        self.calls.append(("ensure_primary_ip", name, alias))
        if self.ips.get(name) is False:
            raise CnsrError(f"Hetzner 项目里已有同名 Primary IP {name}，但它不是 cnsr 创建的。")
        if name not in self.ips:
            self.ips[name], self.assigned[name] = True, None
            if self.fresh_addresses:
                self.addresses[name] = self.fresh_addresses.pop(0)
        return self.addresses.get(name, HZ_IP)

    def unassign_primary_ip(self, name: str) -> None:
        self.calls.append(("unassign_primary_ip", name))
        server = self.assigned.get(name)
        if server is not None and server not in self.off:
            raise CnsrError("服务器还在运行，这个操作要求先关机")
        self.assigned[name] = None

    def create_assigned_primary_ip(self, name: str, alias: str, server: str) -> str:
        self.calls.append(("create_assigned_primary_ip", name, alias, server))
        if server not in self.off:
            raise CnsrError("服务器还在运行，这个操作要求先关机")
        if name in self.ips or server in self.assigned.values():
            raise CnsrError(f"Primary IP {name} 重名，或服务器 {server} 已有 IPv4。")
        taken = {self.addresses.get(ip, HZ_IP) for ip in self.ips}
        address = self.fresh_addresses.pop(0) if self.fresh_addresses else next(
            f"198.51.100.{n}" for n in range(41, 255) if f"198.51.100.{n}" not in taken)
        self.ips[name], self.assigned[name], self.addresses[name] = True, server, address
        return address

    def rename_primary_ip(self, name: str, new_name: str) -> None:
        self.calls.append(("rename_primary_ip", name, new_name))
        self.ips[new_name], self.assigned[new_name] = self.ips.pop(name), self.assigned.pop(name, None)
        if name in self.addresses:
            self.addresses[new_name] = self.addresses.pop(name)

    def delete_primary_ip(self, name: str, alias: str) -> list[str]:
        self.calls.append(("delete_primary_ip", name, alias))
        if name not in self.ips:
            return []
        if not self.ips[name]:
            return [f"Primary IP {name} 不是 cnsr 创建的，未删除"]
        if self.assigned.get(name) is not None:
            return [f"Primary IP {name} 还分配在一台服务器上，未删除"]
        if self.delete_ip_errors:
            return list(self.delete_ip_errors)
        self._drop_ip(name)
        return []

    # --- firewalls

    def ensure_firewall(self, name: str, alias: str) -> None:
        self.calls.append(("ensure_firewall", name, alias))
        if self.firewalls.get(name) is False:
            raise CnsrError(f"Hetzner 项目里已有同名防火墙 {name}，但它不是 cnsr 创建的。")
        self.firewalls[name] = True

    def attach_firewall(self, name: str, server: str) -> None:
        self.calls.append(("attach_firewall", name, server))
        self.attached[name] = server

    def set_rules(self, name: str, ssh_ports, app_ports: tuple[tuple[int, str], ...]) -> None:
        self.calls.append(("set_rules", name, tuple(ssh_ports), tuple(app_ports)))
        self.rules[name] = (tuple(ssh_ports), tuple(app_ports))

    def delete_firewall(self, alias: str) -> list[str]:
        self.calls.append(("delete_firewall", alias))
        if self.delete_firewall_errors:
            return list(self.delete_firewall_errors)
        errors = []
        for name in [name for name, ours in self.firewalls.items() if ours]:
            if name in self.attached:
                errors.append(f"防火墙 {name} 还挂在其他资源上，Hetzner 拒绝删除，请到 Hetzner 控制台处理")
                continue
            self.firewalls.pop(name)
            self.rules.pop(name, None)
        return errors

    def _drop_ip(self, name: str) -> None:
        self.ips.pop(name, None)
        self.assigned.pop(name, None)
        self.addresses.pop(name, None)
