"""Tests for what the bundled preflight.sh prints, run against fake server tools (see preflight_harness)."""

from __future__ import annotations

import os
import sys

import pytest
from preflight_harness import SCRIPT, facts_of, failing, fake_tool, run  # noqa: F401  (fake_tool and run are fixtures)
from preflight_samples import (
    CLEAN_FAKES,
    CLEAN_LINES,
    DEBIAN_12,
    DOCKER_ACTIVE,
    IP_CLEAN,
    IP_COMMAND,
    IPTABLES_DEFAULT,
    IPTABLES_RULES,
    LISTEN_LINES,
    NFT_DEFAULT,
    NFT_RULES,
    PROBES,
    REQUIRED,
    SS_CLEAN,
    SS_UDP_CLEAN,
    SSHD_CONFIG,
    UFW_ACTIVE,
)
from ssh_fakes import CONFIG, T, FakeSsh

from cnsr.errors import CnsrError
from cnsr.server.preflight import evaluate, parse_facts
from cnsr.ssh.remote import Remote

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

# --- the clean server --------------------------------------------------------------------


def test_clean_debian_prints_the_documented_keys_in_order(run):
    out = run()
    assert out.done.returncode == 0 and out.done.stderr == b""
    assert out.lines == CLEAN_LINES
    assert evaluate(facts_of(out), ssh_port=22, will_harden=True) == []


def test_probes_run_under_a_time_limit_when_timeout_exists(run):
    assert run().time_limited == [f"20 {probe}" for probe in PROBES]


def test_the_output_is_the_same_without_timeout(run):
    out = run(without=("timeout",))
    assert out.lines == CLEAN_LINES and out.time_limited == []


def test_the_ssh_port_argument_is_optional(run):
    assert run(args=()).lines == CLEAN_LINES


# --- never disturbs the session, whatever the server lacks -------------------------------


def test_tools_never_see_the_script_on_stdin(run):
    out = run()
    assert out.done.returncode == 0 and out.lines == CLEAN_LINES and not out.stdin_leaked


def test_probes_run_with_the_c_locale_and_see_the_sbin_directories(run):
    out = run()
    assert not out.locale_not_c
    assert out.path_seen.endswith(":/usr/local/sbin:/usr/sbin:/sbin") and out.path_seen.startswith(str(out.base / "bin"))


def test_every_optional_probe_failing_still_exits_zero_with_facts_for_nothing_there(run):
    out = run(failing(*(command for command in CLEAN_FAKES if command not in REQUIRED)), os_release=None, sshd_config=None)
    assert out.done.returncode == 0 and out.done.stderr == b""
    assert out.lines == ["os_id=", "os_version=", "arch=", "user=", *LISTEN_LINES, "containers=0", "admin_exists=0", "ipv6="]
    assert evaluate(facts_of(out), ssh_port=22, will_harden=True)[0].startswith("不支持的系统")


def test_tools_that_are_not_installed_count_as_nothing_there(run):
    missing = {command: ("", 127, f"{command.split()[0]}: command not found\n") for command in CLEAN_FAKES if not command.startswith(("ss", "uname", "id -un"))}
    out = run({**missing, "id admin": ("", 127, "id: command not found\n")}, without=("timeout",))
    assert out.done.returncode == 0 and out.done.stderr == b""
    assert out.lines == [*CLEAN_LINES[:4], *LISTEN_LINES, "containers=0", "admin_exists=0", "ipv6="]


# --- blind without the listener list: stop instead of reporting "nothing listening" ------

BLIND = "无法读取服务器的监听端口"


@pytest.mark.parametrize("command", REQUIRED)
@pytest.mark.parametrize("status", [1, 2, 124, 127, 255])
def test_a_failing_ss_stops_the_preflight_with_exit_1_and_a_chinese_message(run, command, status):
    out = run({command: ("", status, "ss: boom\n")})
    assert out.done.returncode == 1 and out.done.stdout == b""
    assert BLIND in out.done.stderr.decode() and "ss:" not in out.done.stderr.decode()


@pytest.mark.parametrize("command", REQUIRED)
def test_a_timed_out_ss_is_reported_like_a_missing_one(run, command):
    out = run({command: ("LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n", 124)})  # timeout(1) exits 124, whatever was printed
    assert out.done.returncode == 1 and BLIND in out.done.stderr.decode()


@pytest.mark.skipif(any(os.path.exists(f"{d}/ss") for d in ("/usr/local/sbin", "/usr/sbin", "/sbin")), reason="the host has ss in an sbin directory")
def test_a_missing_ss_stops_the_preflight(run):
    out = run(without=("ss",))
    assert out.done.returncode == 1 and out.done.stdout == b"" and BLIND in out.done.stderr.decode()


def test_ss_with_nothing_listening_is_not_blind(run):
    out = run({"ss -tln": SS_CLEAN.splitlines()[0] + "\n", "ss -uln": SS_UDP_CLEAN.splitlines()[0] + "\n"})
    assert out.done.returncode == 0 and out.facts("listen=") == [] and out.facts("udp_listen=") == []


def test_the_message_reaches_the_user_through_remote(run):
    err = run({"ss -tln": ("", 127)}).done.stderr
    with pytest.raises(CnsrError, match=BLIND):
        Remote(T, CONFIG, run=FakeSsh(returncode=1, stderr=err)).run_script("preflight.sh", ["22"])


def test_a_failing_last_probe_does_not_change_the_exit_status(run):
    assert run({IP_COMMAND: ("", 255)}).done.returncode == 0


# --- os, arch, user ----------------------------------------------------------------------


@pytest.mark.parametrize("content,expected", [
    (DEBIAN_12, ("debian", "12")),
    ('NAME="Ubuntu"\nVERSION_ID="22.04"\nID=ubuntu\nID_LIKE=debian\n', ("ubuntu", "22.04")),
    ("ID='debian'\nVERSION_ID='13'\n", ("debian", "13")),
    ("ID=debian\nVERSION_CODENAME=trixie\n", ("debian", "")),
    ("", ("", "")),
])
def test_os_release(run, content, expected):
    assert run(os_release=content).lines[:2] == [f"os_id={expected[0]}", f"os_version={expected[1]}"]


def test_os_release_without_a_final_newline(run):
    assert run(os_release="ID=debian\nVERSION_ID=12").lines[:2] == ["os_id=debian", "os_version=12"]


def test_os_release_with_windows_line_endings(run):
    assert run(os_release="ID=debian\r\nVERSION_ID=\"12\"\r\n").lines[:2] == ["os_id=debian", "os_version=12"]


def test_os_release_is_read_not_executed(run, tmp_path):
    marker = tmp_path / "executed"
    out = run(os_release=f"ID=debian\nVERSION_ID=12\ntouch {marker}\nNAME=$(touch {marker})\n")
    assert out.lines[:2] == ["os_id=debian", "os_version=12"] and not marker.exists()


def test_arch_and_user_come_from_uname_and_id(run):
    assert run({"uname -m": "aarch64\n", "id -un": "admin\n"}).lines[2:4] == ["arch=aarch64", "user=admin"]


# --- listeners ---------------------------------------------------------------------------


def test_listeners_are_the_local_address_column_as_printed(run):
    ss = SS_CLEAN + "LISTEN 0 511 *:8080 *:*\nLISTEN 0 5 [fe80::1]%eth0:5353 [::]:*\nLISTEN 0 128 [::1]:631 [::]:*\n"
    assert run({"ss -tln": ss}).facts("listen=") == [
        "listen=0.0.0.0:22", "listen=[::]:22", "listen=127.0.0.53%lo:53", "listen=*:8080", "listen=[fe80::1]%eth0:5353", "listen=[::1]:631",
    ]  # fmt: skip


def test_only_listening_sockets_count(run):
    ss = SS_CLEAN + "ESTAB  0 0 192.0.2.10:22 198.51.100.7:50000\nTIME-WAIT 0 0 192.0.2.10:443 203.0.113.9:40000\n"
    assert run({"ss -tln": ss}).facts("listen=") == CLEAN_LINES[4:7]


def test_udp_listeners_are_reported_apart_from_tcp(run):
    udp = SS_UDP_CLEAN + "UNCONN 0 0 *:443 *:*\nUNCONN 0 0 [::]:546 [::]:*\nESTAB 0 0 192.0.2.10:41000 198.51.100.7:53\n"
    out = run({"ss -uln": udp})
    assert out.facts("udp_listen=") == [
        "udp_listen=127.0.0.53%lo:53", "udp_listen=0.0.0.0:68", "udp_listen=*:443", "udp_listen=[::]:546",
    ]  # fmt: skip
    assert out.facts("listen=") == CLEAN_LINES[4:7]
    assert evaluate(facts_of(out), ssh_port=22, will_harden=True) == ["443 端口已被占用"]


# --- containers --------------------------------------------------------------------------


def test_containers_are_counted_from_docker_ps(run):
    out = run({"docker ps -q": "3f2a9c1d7b44\n9b0e5d8a1c27\n"})
    assert "containers=2" in out.lines
    assert evaluate(facts_of(out), ssh_port=22, will_harden=True) == ["已有 2 个容器在运行"]


def test_a_docker_that_is_not_running_is_not_started_by_asking_it(run):
    out = run({DOCKER_ACTIVE: ("", 3), "docker ps -q": "3f2a9c1d7b44\n"})
    assert "containers=0" in out.lines
    assert "20 docker ps -q" not in out.time_limited


def test_without_systemd_docker_is_asked_directly(run):
    out = run({"docker ps -q": "3f2a9c1d7b44\n"}, without=("systemctl",))
    assert "containers=1" in out.lines


def test_docker_without_a_daemon_counts_as_no_containers(run):
    out = run({"docker ps -q": ("", 1, "Cannot connect to the Docker daemon\n")})
    assert "containers=0" in out.lines and out.done.stderr == b""


# --- firewall ----------------------------------------------------------------------------


def test_active_ufw_prints_one_line_per_rule_with_single_spaces(run):
    out = run({"ufw status": UFW_ACTIVE})
    assert out.facts("fw=") == [
        "fw=22/tcp ALLOW Anywhere", "fw=443/tcp ALLOW Anywhere", "fw=22/tcp (v6) ALLOW Anywhere (v6)", "fw=OpenSSH (v6) ALLOW Anywhere (v6)",
    ]  # fmt: skip
    assert evaluate(facts_of(out), ssh_port=22, will_harden=True) == ["已有防火墙规则：443/tcp ALLOW Anywhere"]


def test_active_ufw_with_no_rules_prints_nothing(run):
    assert run({"ufw status": "Status: active\n"}).facts("fw=") == []


def test_inactive_ufw_falls_through_to_nft_and_iptables(run):
    out = run({"ufw status": "Status: inactive\n", "nft list ruleset": NFT_RULES, "iptables-save": IPTABLES_RULES})
    assert out.facts("fw=") == ["fw=nft: 4 rules", "fw=iptables: 3 rules"]


def test_ufw_manages_nft_and_iptables_so_only_its_rules_are_reported(run):
    out = run({"ufw status": UFW_ACTIVE, "nft list ruleset": NFT_RULES, "iptables-save": IPTABLES_RULES})
    assert len(out.facts("fw=")) == 4 and not any("nft" in line or "iptables" in line for line in out.facts("fw="))


def test_default_empty_rulesets_print_nothing(run):
    fakes = {"nft list ruleset": NFT_DEFAULT, "iptables-save": IPTABLES_DEFAULT, "ip6tables-save": IPTABLES_DEFAULT}
    assert run(fakes).lines == CLEAN_LINES


def test_nft_rules_are_summarised_in_one_line(run):
    assert run({"nft list ruleset": NFT_RULES}).facts("fw=") == ["fw=nft: 4 rules"]


def test_iptables_rules_and_drop_policies_are_summarised_in_one_line(run):
    assert run({"iptables-save": IPTABLES_RULES}).facts("fw=") == ["fw=iptables: 3 rules"]


def test_ip6tables_rules_count_towards_the_iptables_summary(run):
    ipv6_rules = "*filter\n:INPUT ACCEPT [0:0]\n-A INPUT -p tcp --dport 22 -j ACCEPT\nCOMMIT\n"
    assert run({"iptables-save": IPTABLES_RULES, "ip6tables-save": ipv6_rules}).facts("fw=") == ["fw=iptables: 4 rules"]
    assert run({"ip6tables-save": ipv6_rules}).facts("fw=") == ["fw=iptables: 1 rules"]


# --- admin, sshd_config, ipv6 ------------------------------------------------------------


def test_admin_exists(run):
    present = run({"id admin": "uid=1000(admin) gid=1000(admin)\n"})
    assert "admin_exists=1" in present.lines and "admin_exists=0" not in present.lines
    assert "admin_exists=0" in run().lines


def test_uncommented_port_lines_are_printed_with_single_spaces(run):
    out = run(sshd_config=SSHD_CONFIG)
    assert out.facts("port_line=") == ["port_line=Port 2222", "port_line=port 2200", "port_line=Port=2300"]
    assert "sshd_config" in evaluate(facts_of(out), ssh_port=22, will_harden=True)[0]


def test_port_lines_in_sshd_config_d_drop_ins_count_after_those_of_sshd_config(run):
    drop_ins = {"50-vendor.conf": "# Port 2200\nPort 22\nPasswordAuthentication yes\n", "20-first.conf": "port\t2022\n", "README": "Port 1\n",
                "60-old.conf.bak": "Port 3\n"}
    out = run(sshd_config=SSHD_CONFIG, sshd_config_d=drop_ins)
    assert out.facts("port_line=") == [
        "port_line=Port 2222", "port_line=port 2200", "port_line=Port=2300", "port_line=port 2022", "port_line=Port 22",
    ]  # fmt: skip


def test_a_vendor_drop_in_with_port_22_is_a_finding(run):
    out = run(sshd_config_d={"50-cloud-init.conf": "Port 22\n"})
    assert out.facts("port_line=") == ["port_line=Port 22"]
    assert "sshd_config" in evaluate(facts_of(out), ssh_port=22, will_harden=True)[0]


def test_the_cnsr_drop_in_is_not_a_hard_coded_port(run):
    out = run(sshd_config_d={"10-cnsr.conf": "Port 22\nPort 23456\n"})
    assert out.facts("port_line=") == [] and out.lines == CLEAN_LINES


def test_drop_in_files_with_glob_characters_in_their_lines_are_read_literally(run):
    assert run(sshd_config_d={"50-x.conf": "Port *\n"}).facts("port_line=") == ["port_line=Port *"]


def test_wildcards_in_the_output_are_not_expanded_to_file_names(run):
    out = run({"ufw status": UFW_ACTIVE + "Anywhere DENY *\n"}, sshd_config="Port *\n")
    assert "fw=Anywhere DENY *" in out.lines and out.facts("port_line=") == ["port_line=Port *"]


def test_port_line_with_windows_line_endings_keeps_the_carriage_return_out_of_the_fact(run):
    assert run(sshd_config="Port 2222\r\n").facts("port_line=") == ["port_line=Port 2222"]


def test_ipv6_is_the_first_stable_global_address(run):
    ip = (
        "3: docker0    inet6 fd00:db8::1/64 scope global \\       valid_lft forever preferred_lft forever\n"
        "2: eth0    inet6 2001:db8::99/64 scope global temporary dynamic \\       valid_lft 600sec preferred_lft 300sec\n"
        "2: eth0    inet6 2001:db8::98/64 scope global deprecated dynamic \\       valid_lft 600sec preferred_lft 0sec\n"
        + IP_CLEAN
        + "2: eth0    inet6 2001:db8::11/64 scope global dynamic \\       valid_lft 600sec preferred_lft 300sec\n"
    )
    assert run({IP_COMMAND: ip}).facts("ipv6=") == ["ipv6=2001:db8::10"]


def test_ipv6_is_empty_when_there_is_no_global_address(run):
    assert run({IP_COMMAND: ""}).lines[-1] == "ipv6="


# --- the real asset ----------------------------------------------------------------------


def test_remote_streams_the_bundled_script_with_the_ssh_port():
    fake = FakeSsh(stdout="\n".join(CLEAN_LINES))
    out = Remote(T, CONFIG, run=fake).run_script("preflight.sh", ["22"])
    assert fake.kwargs["input"] == SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    assert fake.args[-1] == "timeout -k 10 135 bash -s -- 22" and parse_facts(out).os_id == "debian"
