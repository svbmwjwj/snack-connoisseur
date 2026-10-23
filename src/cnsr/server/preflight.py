"""Reading what ``preflight.sh`` found on a server and deciding whether cnsr may take it over.

The script prints ``key=value`` lines and changes nothing on the server. ``parse_facts``
turns them into ``Facts``; ``evaluate`` lists everything that says the server is not a fresh
system. Any finding stops the deployment before cnsr touches the server.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from cnsr.errors import CnsrError

SUPPORTED = {("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04")}
SUPPORTED_ARCH = "x86_64"
_XRAY_PORT = 443
# The LLMNR responder systemd-resolved opens on every address of a fresh Debian 13 (Lightsail's
# image included). The cloud or host firewall keeps it closed, so it is part of a fresh system.
_SYSTEM_PORTS = frozenset({5355})
_TEXT_KEYS = ("os_id", "os_version", "arch", "user", "ipv6")
_LIST_KEYS = ("listen", "udp_listen", "fw", "port_line")
_DIGITS = re.compile(r"[0-9]+")
# A ufw rule as `ufw status` prints it: "22/tcp ALLOW Anywhere", "22/tcp (v6) ALLOW Anywhere (v6)".
_UFW_PORT = re.compile(r"([0-9]+)(?:/(?:tcp|udp))?")
_SSH_PROFILE = "OpenSSH"  # ufw's application profile for 22/tcp


@dataclass
class Facts:
    """What ``preflight.sh`` printed; every field is named after its key."""

    os_id: str = ""
    os_version: str = ""
    arch: str = ""
    user: str = ""
    listen: list[str] = field(default_factory=list)
    udp_listen: list[str] = field(default_factory=list)
    containers: int = 0
    fw: list[str] = field(default_factory=list)
    admin_exists: bool = False
    port_line: list[str] = field(default_factory=list)
    ipv6: str = ""


def parse_facts(text: str) -> Facts:
    """Read the script's output. Lines that are not ``key=value`` with a known key are ignored."""
    facts = Facts()
    for line in text.split("\n"):
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator:
            continue
        if key in _TEXT_KEYS:
            setattr(facts, key, value)
        elif key in _LIST_KEYS:
            if value:
                getattr(facts, key).append(value)
        elif key == "containers":
            facts.containers = _number(key, value)
        elif key == "admin_exists":
            facts.admin_exists = _flag(key, value)
    return facts


def evaluate(f: Facts, *, ssh_port: int, will_harden: bool) -> list[str]:
    """Everything that stops a takeover, as Chinese messages; an empty list means go ahead.

    ``will_harden`` is False for servers cnsr creates itself (AWS), where the checks that
    only matter for changing SSH settings are skipped.
    """
    findings: list[str] = []
    if (f.os_id, f.os_version) not in SUPPORTED or f.arch != SUPPORTED_ARCH:
        findings.append(
            f"不支持的系统：{f.os_id} {f.os_version} {f.arch}（只支持 Debian 12、13 和 Ubuntu 22.04、24.04，x86-64）"
        )
    listeners = [parsed for address in f.listen if (parsed := _split_address(address))]
    udp = [parsed for address in f.udp_listen if (parsed := _split_address(address))]
    # Xray takes 443 over TCP, and over UDP where it is open to the world; other UDP is fine.
    if any(port == _XRAY_PORT for _, port in listeners) or any(
        port == _XRAY_PORT and not _is_loopback(host) for host, port in udp
    ):
        findings.append("443 端口已被占用")
    others = sorted({
        port for host, port in listeners
        if not _is_loopback(host) and port not in (ssh_port, _XRAY_PORT) and port not in _SYSTEM_PORTS
    })
    if others:
        findings.append("除 SSH 外还有其他服务在监听：" + "、".join(map(str, others)))
    if f.containers > 0:
        findings.append(f"已有 {f.containers} 个容器在运行")
    rules = [rule for rule in f.fw if not _only_allows_ssh(rule, ssh_port)]
    if rules:
        findings.append("已有防火墙规则：" + "；".join(rules))
    if will_harden and f.admin_exists:
        findings.append("用户 admin 已存在")
    if will_harden and f.port_line:
        findings.append("sshd_config 里写死了 SSH 端口，无法安全地改端口")
    if will_harden and ssh_port != 22:  # harden-stage.sh keeps 22 open next to the new port
        findings.append("SSH 不在 22 端口，无法安全加固")
    return findings


def _number(key: str, value: str) -> int:
    if not _DIGITS.fullmatch(value):
        raise CnsrError(f"服务器体检结果里 {key} 不是数字：{value!r}")
    return int(value)


def _flag(key: str, value: str) -> bool:
    if value not in ("0", "1"):
        raise CnsrError(f"服务器体检结果里 {key} 不是 0 或 1：{value!r}")
    return value == "1"


def _split_address(address: str) -> tuple[str, int] | None:
    """Split ss's local address ("[::]:22", "127.0.0.53%lo:53", "*:80") into host and port."""
    host, separator, port = address.strip().rpartition(":")
    if not separator or not _DIGITS.fullmatch(port):
        return None
    # The zone can sit inside the brackets ("[fe80::1%eth0]") or after them ("[fe80::1]%eth0").
    return host.split("%")[0].strip("[]"), int(port)


def _is_loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:  # "*" or a name: listens on more than loopback
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _only_allows_ssh(rule: str, ssh_port: int) -> bool:
    """True for a ufw line that merely opens ``ssh_port`` (the port cnsr already uses).

    ``ufw allow OpenSSH`` and ``ufw limit ssh`` are the usual ways to write it: the first is
    ufw's profile for port 22, the second an allow with a connection rate limit.
    """
    words = rule.split()
    if not words:
        return False
    if words[0] == _SSH_PROFILE:
        opens_ssh = ssh_port == 22
    else:
        port = _UFW_PORT.fullmatch(words[0])
        opens_ssh = bool(port) and int(port[1]) == ssh_port
    action = words[2:] if words[1:2] == ["(v6)"] else words[1:]
    return opens_ssh and action[:1] in (["ALLOW"], ["LIMIT"])
