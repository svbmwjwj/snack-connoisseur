"""A fake Lightsail region for the ``cnsr add`` and ``cnsr rm`` tests."""

from __future__ import annotations

from cnsr.errors import CnsrError

AWS_IP = "198.51.100.20"
AWS_IPV6 = "2001:db8::20"


class FakeLightsail:
    """The Lightsail of one region: it records its calls and holds the instances and static IPs.

    ``instances`` maps an instance name to whether it carries cnsr's tag, ``ips`` a static IP
    name to the instance it is attached to (None: free). The ``ensure_*`` calls fill them in
    and refuse a stranger's resource like the real ones; a test puts a stranger's in first.
    ``release_errors`` and ``delete_instance_errors`` are what the removal calls return (the
    resource is then left in place). ``addresses`` gives static IPs their own address
    (``AWS_IP`` otherwise); only those are found by address. With ``attach_error`` set,
    ``ensure_static_ip`` allocates the IP and then fails to attach it, as AWS may.
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

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def ensure_key_pair(self, name: str, pub_line: str) -> str:
        self.calls.append(("ensure_key_pair", name, pub_line))
        return name

    def ensure_instance(self, name: str, key_pair: str) -> bool:
        self.calls.append(("ensure_instance", name, key_pair))
        if self.instances.get(name) is False:
            raise CnsrError(f"已有同名实例 {name}，但它不是 cnsr 创建的。")
        created = name not in self.instances
        self.instances[name] = True
        return created

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

    def set_firewall(self, name: str) -> None:
        self.calls.append(("set_firewall", name))

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
