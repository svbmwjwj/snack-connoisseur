"""Tests for reading the server preflight facts and the takeover rules."""

from __future__ import annotations

import pytest

from cnsr.errors import CnsrError
from cnsr.server.preflight import SUPPORTED, Facts, evaluate, parse_facts
from cnsr.server.xray import PORTS

CLEAN = (
    "os_id=debian\nos_version=12\narch=x86_64\nuser=root\n"
    "listen=0.0.0.0:22\nlisten=[::]:22\nlisten=127.0.0.53:53\n"
    "containers=0\nadmin_exists=0\nipv6=2001:db8::10\n"
)
UNSUPPORTED = "不支持的系统：{}（只支持 Debian 12、13 和 Ubuntu 22.04、24.04，x86-64）"


def with_extra(extra: str) -> str:
    """CLEAN with more fact lines inserted after ``containers``."""
    return CLEAN.replace("containers=0\n", "containers=0\n" + extra)


def check(text: str = CLEAN, *, ssh_port: int = 22, will_harden: bool = True) -> list[str]:
    return evaluate(parse_facts(text), ssh_port=ssh_port, will_harden=will_harden, app_ports=PORTS)


# --- parse_facts -------------------------------------------------------------------------


def test_parse_repeated_keys():
    f = parse_facts(CLEAN)
    assert f.listen == ["0.0.0.0:22", "[::]:22", "127.0.0.53:53"] and f.ipv6 == "2001:db8::10"


def test_parse_converts_types():
    f = parse_facts(with_extra("fw=22/tcp ALLOW Anywhere\nfw=nft: 3 rules\nport_line=Port 22\n").replace("admin_exists=0", "admin_exists=1"))
    assert (f.os_id, f.os_version, f.arch, f.user) == ("debian", "12", "x86_64", "root")
    assert f.containers == 0 and f.admin_exists is True
    assert f.fw == ["22/tcp ALLOW Anywhere", "nft: 3 rules"] and f.port_line == ["Port 22"]


def test_parse_defaults_when_the_output_is_empty():
    assert parse_facts("") == Facts()
    assert Facts() == Facts(os_id="", os_version="", arch="", user="", listen=[], containers=0, fw=[],
                            admin_exists=False, port_line=[], ipv6="")


def test_parse_tolerates_crlf_blank_lines_noise_and_empty_values():
    text = "Welcome to a server\r\n\r\nos_id=debian\r\nmotd: a=b is not a fact\r\nfoo=bar\r\nlisten=\r\nipv6=\r\ncontainers=1\r\n"
    f = parse_facts(text)
    assert (f.os_id, f.listen, f.ipv6, f.containers) == ("debian", [], "", 1)


def test_parse_keeps_everything_after_the_first_equals_sign_and_the_last_scalar_wins():
    f = parse_facts("port_line=Port=2222\nos_id=ubuntu\nos_id=debian\n")
    assert f.port_line == ["Port=2222"] and f.os_id == "debian"


@pytest.mark.parametrize("line", ["containers=two", "containers=", "containers=-1", "containers=²", "admin_exists=yes", "admin_exists=2"])
def test_parse_rejects_a_value_that_is_not_a_number_or_flag(line):
    with pytest.raises(CnsrError, match=line.split("=")[0]):
        parse_facts(line)


# --- evaluate: the clean path and the order ----------------------------------------------


def test_clean_server_has_no_findings():
    assert check() == []


def test_supported_systems():
    assert SUPPORTED == {("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04")}
    for os_id, version in sorted(SUPPORTED):
        assert check(CLEAN.replace("os_id=debian", f"os_id={os_id}").replace("os_version=12", f"os_version={version}")) == []


def test_every_finding_with_exact_text_in_table_order():
    text = (
        "os_id=debian\nos_version=11\narch=aarch64\nuser=root\n"
        "listen=0.0.0.0:22\nlisten=0.0.0.0:443\nlisten=[::]:8080\nlisten=0.0.0.0:3306\n"
        "containers=2\nfw=3306/tcp ALLOW Anywhere\nfw=nft: 12 rules\nadmin_exists=1\nport_line=Port 22\n"
    )
    assert check(text) == [
        UNSUPPORTED.format("debian 11 aarch64"),
        "443 端口已被占用",
        "除 SSH 外还有其他服务在监听：3306、8080",
        "已有 2 个容器在运行",
        "已有防火墙规则：3306/tcp ALLOW Anywhere；nft: 12 rules",
        "用户 admin 已存在",
        "sshd_config 里写死了 SSH 端口，无法安全地改端口",
    ]


# --- evaluate: one rule at a time --------------------------------------------------------


@pytest.mark.parametrize("extra,needle", [
    ("listen=0.0.0.0:443\n", "443 端口已被占用"),
    ("listen=0.0.0.0:8080\n", "除 SSH 外还有其他服务在监听：8080"),
    ("fw=ALLOW 3306/tcp\n", "已有防火墙规则"),
    ("port_line=Port 22\n", "写死了 SSH 端口"),
])
def test_findings(extra, needle):
    out = evaluate(parse_facts(with_extra(extra)), ssh_port=22, will_harden=True, app_ports=PORTS)
    assert any(needle in s for s in out)


def test_containers_finding():
    assert check(CLEAN.replace("containers=0", "containers=2")) == ["已有 2 个容器在运行"]


@pytest.mark.parametrize("os_id,version,arch", [
    ("debian", "11", "x86_64"), ("debian", "12", "aarch64"), ("ubuntu", "20.04", "x86_64"),
    ("ubuntu", "12", "x86_64"), ("fedora", "40", "x86_64"), ("", "", ""),
])
def test_unsupported_os(os_id, version, arch):
    text = CLEAN.replace("os_id=debian", f"os_id={os_id}").replace("os_version=12", f"os_version={version}")
    text = text.replace("arch=x86_64", f"arch={arch}")
    assert check(text) == [UNSUPPORTED.format(f"{os_id} {version} {arch}")]


def test_unsupported_os_message_text():
    assert check(CLEAN.replace("os_version=12", "os_version=11")) == [
        "不支持的系统：debian 11 x86_64（只支持 Debian 12、13 和 Ubuntu 22.04、24.04，x86-64）"
    ]


def test_hardening_needs_ssh_on_port_22():
    on_2222 = CLEAN.replace("0.0.0.0:22", "0.0.0.0:2222").replace("[::]:22", "[::]:2222")
    assert check(on_2222, ssh_port=2222, will_harden=True) == ["SSH 不在 22 端口，无法安全加固"]
    assert check(on_2222, ssh_port=2222, will_harden=False) == []


def test_aws_skips_harden_only_checks():
    text = with_extra("port_line=Port 2222\n").replace("admin_exists=0", "admin_exists=1")
    assert check(text, will_harden=False) == []
    assert check(text, will_harden=True) == ["用户 admin 已存在", "sshd_config 里写死了 SSH 端口，无法安全地改端口"]


# --- evaluate: listeners -----------------------------------------------------------------


def test_443_is_reported_alone_whatever_address_it_is_on():
    for address in ("0.0.0.0:443", "[::]:443", "*:443", "127.0.0.1:443"):
        assert check(with_extra(f"listen={address}\n")) == ["443 端口已被占用"]


@pytest.mark.parametrize("address", [
    "127.0.0.1:631", "127.0.0.53%lo:53", "127.1.2.3:8080", "[::1]:631", "[::1%lo]:631", "[::ffff:127.0.0.1]:8080",
])
def test_loopback_listeners_are_ignored(address):
    assert check(with_extra(f"listen={address}\n")) == []


@pytest.mark.parametrize("address", [
    "0.0.0.0:8080", "[::]:8080", "*:8080", "192.0.2.5:8080", "[2001:db8::5]:8080", "[fe80::1]%eth0:8080", "[fe80::1%eth0]:8080",
    "[::ffff:192.0.2.5]:8080", "localhost:8080",
])
def test_other_listeners_are_other_services(address):
    assert check(with_extra(f"listen={address}\n")) == ["除 SSH 外还有其他服务在监听：8080"]


def test_the_llmnr_responder_of_a_fresh_debian_13_is_not_another_service():
    # systemd-resolved on a fresh Lightsail Debian 13 image, as `ss -tln` prints it
    extra = "listen=127.0.0.53%lo:53\nlisten=0.0.0.0:5355\nlisten=127.0.0.54:53\nlisten=[::]:5355\n"
    assert check(with_extra(extra)) == []


def test_llmnr_is_left_out_of_a_finding_about_other_services():
    extra = "listen=0.0.0.0:5355\nlisten=0.0.0.0:8080\n"
    assert check(with_extra(extra)) == ["除 SSH 外还有其他服务在监听：8080"]


def test_listener_ports_are_deduplicated_and_sorted_numerically():
    extra = "listen=0.0.0.0:9000\nlisten=[::]:9000\nlisten=0.0.0.0:80\nlisten=*:111\n"
    assert check(with_extra(extra)) == ["除 SSH 外还有其他服务在监听：80、111、9000"]


def test_malformed_listen_values_are_ignored():
    assert check(with_extra("listen=garbage\nlisten=0.0.0.0:*\nlisten=0.0.0.0:\n")) == []


def test_the_ssh_port_comes_from_the_caller():
    on_2222 = CLEAN.replace("0.0.0.0:22", "0.0.0.0:2222").replace("[::]:22", "[::]:2222")
    assert check(on_2222, ssh_port=2222, will_harden=False) == []
    assert check(on_2222, ssh_port=22) == ["除 SSH 外还有其他服务在监听：2222"]
    assert check(CLEAN, ssh_port=2222, will_harden=False) == ["除 SSH 外还有其他服务在监听：22"]


# --- evaluate: UDP listeners -------------------------------------------------------------


def test_parse_udp_listeners():
    assert parse_facts(with_extra("udp_listen=0.0.0.0:68\nudp_listen=*:443\n")).udp_listen == ["0.0.0.0:68", "*:443"]


@pytest.mark.parametrize("address", ["0.0.0.0:443", "[::]:443", "*:443", "192.0.2.5:443"])
def test_udp_443_is_the_port_taken(address):
    assert check(with_extra(f"udp_listen={address}\n")) == ["443 端口已被占用"]


def test_udp_on_loopback_and_other_udp_ports_are_not_findings():
    extra = "udp_listen=127.0.0.1:443\nudp_listen=[::1]:443\nudp_listen=0.0.0.0:68\nudp_listen=127.0.0.53%lo:53\nudp_listen=[::]:546\n"
    assert check(with_extra(extra)) == []


def test_443_over_tcp_and_udp_is_one_finding():
    assert check(with_extra("listen=0.0.0.0:443\nudp_listen=0.0.0.0:443\n")) == ["443 端口已被占用"]


# --- evaluate: firewall ------------------------------------------------------------------


@pytest.mark.parametrize("rule", [
    "22/tcp ALLOW Anywhere", "22/tcp (v6) ALLOW Anywhere (v6)", "22 ALLOW Anywhere", "22/udp ALLOW Anywhere",
    "22/tcp ALLOW IN 203.0.113.5", "22/tcp ALLOW Anywhere on eth0",
    "OpenSSH ALLOW Anywhere", "OpenSSH (v6) ALLOW Anywhere (v6)", "22/tcp LIMIT Anywhere", "22/tcp (v6) LIMIT Anywhere (v6)",
])
def test_ufw_rules_that_only_allow_the_ssh_port_are_not_findings(rule):
    assert check(with_extra(f"fw={rule}\n")) == []


@pytest.mark.parametrize("rule", [
    "22/tcp DENY Anywhere", "22/tcp REJECT Anywhere", "443/tcp ALLOW Anywhere", "443/tcp LIMIT Anywhere", "3306/tcp ALLOW Anywhere",
    "2222/tcp ALLOW Anywhere", "22:23/tcp ALLOW Anywhere", "OpenSSH DENY Anywhere", "OpenSSH2 ALLOW Anywhere", "Apache ALLOW Anywhere",
    "Anywhere DENY 203.0.113.9", "nft: 12 rules", "iptables: 3 rules",
])
def test_every_other_firewall_line_is_a_finding(rule):
    assert check(with_extra(f"fw={rule}\n")) == [f"已有防火墙规则：{rule}"]


def test_firewall_finding_lists_only_the_foreign_rules():
    extra = "fw=22/tcp ALLOW Anywhere\nfw=3306/tcp ALLOW Anywhere\nfw=22/tcp (v6) ALLOW Anywhere (v6)\nfw=iptables: 3 rules\n"
    assert check(with_extra(extra)) == ["已有防火墙规则：3306/tcp ALLOW Anywhere；iptables: 3 rules"]


def test_ufw_allowing_the_ssh_port_follows_the_ssh_port_argument():
    on_2222 = CLEAN.replace("0.0.0.0:22", "0.0.0.0:2222").replace("[::]:22", "[::]:2222")
    text = on_2222.replace("containers=0\n", "containers=0\nfw=2222/tcp ALLOW Anywhere\n")
    assert check(text, ssh_port=2222, will_harden=False) == []
    assert "已有防火墙规则：2222/tcp ALLOW Anywhere" in check(text, ssh_port=22)


@pytest.mark.parametrize("rule", [
    "OpenSSH ALLOW Anywhere", "OpenSSH (v6) ALLOW Anywhere (v6)", "OpenSSH LIMIT Anywhere", "22/tcp ALLOW Anywhere",
])
def test_the_openssh_profile_means_port_22_only(rule):
    on_2222 = CLEAN.replace("0.0.0.0:22", "0.0.0.0:2222").replace("[::]:22", "[::]:2222")
    text = on_2222.replace("containers=0\n", f"containers=0\nfw={rule}\n")
    assert check(text, ssh_port=2222, will_harden=False) == [f"已有防火墙规则：{rule}"]
    assert check(with_extra(f"fw={rule}\n"), ssh_port=22) == []


def test_limit_on_a_custom_ssh_port_follows_the_ssh_port_argument():
    on_2222 = CLEAN.replace("0.0.0.0:22", "0.0.0.0:2222").replace("[::]:22", "[::]:2222")
    text = on_2222.replace("containers=0\n", "containers=0\nfw=2222/tcp LIMIT Anywhere\nfw=2222/tcp (v6) LIMIT Anywhere (v6)\n")
    assert check(text, ssh_port=2222, will_harden=False) == []
