"""A fake Lightsail region for the ``cnsr up`` and ``cnsr rm`` tests."""

from __future__ import annotations

from decimal import Decimal

from cnsr.aws.static_ips import StaticIp
from cnsr.errors import CnsrError
from cnsr.sizes import Offer, label, pick

AWS_IP = "198.51.100.20"
AWS_IPV6 = "2001:db8::20"
GB = 1024**3


def bundle(name: str, cores: float, memory: float, monthly: str, disk: float = 20) -> Offer:
    return Offer(label(cores, memory), name, cores, memory, disk, 1024 * GB, None, Decimal(monthly), Decimal(0), "USD")


def default_bundles() -> list[Offer]:
    return [
        bundle("nano_3_0", 2, 0.5, "5"), bundle("micro_3_0", 2, 1, "7", 40), bundle("small_3_0", 2, 2, "12", 60),
        bundle("medium_3_0", 2, 4, "24", 80), bundle("large_3_0", 2, 8, "44", 160),
    ]


class FakeLightsail:
    """The Lightsail of one region: it records its calls and holds the instances and static IPs.

    ``instances`` maps an instance name to whether it carries cnsr's tag, ``ips`` a static IP
    name to the instance it is attached to (None: free). The ``ensure_*`` calls fill them in
    and refuse a stranger's resource like the real ones; a test puts a stranger's in first.
    ``release_errors`` and ``delete_instance_errors`` are what the removal calls return (the
    resource is then left in place). ``addresses`` gives static IPs their own address
    (``AWS_IP`` otherwise); only those are found by address. With ``attach_error`` set,
    ``ensure_static_ip`` allocates the IP and then fails to attach it, as AWS may. ``bundles``
    is what ``offers`` lists, and ``created`` maps each instance created to its bundle.
    ``taken`` holds the names other resources of the region have (a key pair, say).
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.instances: dict[str, bool] = {}
        self.ips: dict[str, str | None] = {}
        self.addresses: dict[str, str] = {}
        # The address each new allocation gets in turn (then AWS_IP); a released IP's is gone.
        self.fresh_addresses: list[str] = []
        self.release_errors: list[str] = []
        self.delete_instance_errors: list[str] = []
        self.attach_error: CnsrError | None = None
        self.bundles: list[Offer] = default_bundles()
        self.created: dict[str, str] = {}
        self.taken: set[str] = set()

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def ensure_key_pair(self, name: str, pub_line: str, alias: str | None = None) -> str:
        self.calls.append(("ensure_key_pair", name, pub_line))
        return f"{name}-key" if name == alias else name

    def offers(self) -> list[Offer]:
        self.calls.append(("offers",))
        return list(self.bundles)

    def pick(self, size: str) -> Offer:
        self.calls.append(("pick", size))
        found = pick(self.bundles, size)
        if found is None:
            raise CnsrError(f"没有 {size} 规格的 Lightsail 套餐。")
        return found

    def ensure_instance(self, name, key_pair, size, alias, record, *, numbered=False) -> tuple[str, bool]:
        for number in range(10 if numbered else 1):
            candidate = f"{name}-{number}" if number else name
            if self.instances.get(candidate) is True:
                self.calls.append(("ensure_instance", candidate, key_pair))
                record(candidate)
                return candidate, False
            if candidate in self.instances or candidate in self.taken:
                if numbered:
                    continue
                raise CnsrError(f"已有同名实例或资源 {candidate}。")
            self.calls.append(("ensure_instance", candidate, key_pair))
            record(candidate)
            self.created[candidate] = pick(self.bundles, size).name
            self.instances[candidate] = True
            return candidate, True
        raise CnsrError(f"{name} 到 {name}-9 都已被占用。")

    def wait_running(self, name: str) -> None:
        self.calls.append(("wait_running", name))

    def ensure_static_ip(self, instance: str, ip_name: str) -> str:
        self.calls.append(("ensure_static_ip", instance, ip_name))
        attached = self.ips.get(ip_name)
        if attached not in (None, instance):
            raise CnsrError(f"静态 IP {ip_name} 已经绑在实例 {attached} 上，不是 {instance}，已停止。")
        if ip_name not in self.ips and ip_name not in self.addresses and self.fresh_addresses:
            self.addresses[ip_name] = self.fresh_addresses.pop(0)
        if self.attach_error is not None:
            self.ips.setdefault(ip_name, None)
            raise self.attach_error
        self.ips[ip_name] = instance
        return self.addresses.get(ip_name, AWS_IP)

    def static_ips(self) -> list[StaticIp]:
        self.calls.append(("static_ips",))
        return [StaticIp(name, self.addresses.get(name, AWS_IP), attached) for name, attached in self.ips.items()]

    def find_static_ip(self, address: str) -> tuple[str, str | None] | None:
        self.calls.append(("find_static_ip", address))
        name = next((name for name, value in self.addresses.items() if value == address and name in self.ips), None)
        return None if name is None else (name, self.ips[name])

    def detach_static_ip(self, ip_name: str) -> None:
        self.calls.append(("detach_static_ip", ip_name))
        self.ips[ip_name] = None

    def instance_ipv6(self, name: str) -> str | None:
        self.calls.append(("instance_ipv6", name))
        return AWS_IPV6

    def set_firewall(self, name: str, app_ports: tuple[tuple[int, str], ...]) -> None:
        self.calls.append(("set_firewall", name))
        self.app_ports = app_ports

    def pick_blueprint(self) -> str:
        self.calls.append(("pick_blueprint",))
        return "debian_13"

    def instance_tagged(self, name: str, alias: str) -> bool | None:
        self.calls.append(("instance_tagged", name, alias))
        return self.instances.get(name)

    def static_ip_attachment(self, ip_name: str) -> tuple[bool, str | None]:
        self.calls.append(("static_ip_attachment", ip_name))
        return ip_name in self.ips, self.ips.get(ip_name)

    def release_static_ip(self, ip_name: str) -> list[str]:
        self.calls.append(("release_static_ip", ip_name))
        if self.release_errors:
            return list(self.release_errors)
        self.ips.pop(ip_name, None)
        self.addresses.pop(ip_name, None)
        return []

    def delete_instance(self, name: str) -> list[str]:
        self.calls.append(("delete_instance", name))
        if self.delete_instance_errors:
            return list(self.delete_instance_errors)
        self.instances.pop(name, None)
        self.ips = {ip: (None if attached == name else attached) for ip, attached in self.ips.items()}
        return []
