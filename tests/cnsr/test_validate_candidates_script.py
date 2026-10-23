"""Tests for validate-candidates.sh, run against a fake server (see xray_script_harness)."""

from __future__ import annotations

import sys

import pytest
from xray_script_harness import box, harden_tool  # noqa: F401  (fixtures)

from cnsr.scan.select import Candidate, parse_validation

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

SCRIPT = "validate-candidates.sh"
IP = "198.51.100.7"


def curl(domain: str, ip: str = IP) -> str:
    """The command line the script must use for one pair."""
    return (
        f"curl -sS -o /dev/null --max-time 8 --resolve {domain}:443:{ip} --tlsv1.3 --tls-max 1.3 "
        f"--curves X25519 --http2 -w %{{http_version}} %{{time_appconnect}} https://{domain}/"
    )


def curl_calls(result) -> list[str]:
    return [call for call in result.calls if call.startswith("curl ")]


def test_prints_only_the_pairs_that_pass_in_order(box):
    box.answer(curl("a.example.com"), "2 0.123456")
    box.answer(curl("b.example.com", "198.51.100.8"), "1.1 0.050000")  # no h2
    box.answer(curl("c.example.com", "198.51.100.9"), "2 0.080000", status=28)  # curl failed
    box.answer(curl("d.example.com", "203.0.113.4"), "2 1.250000")
    result = box.run(SCRIPT, "a.example.com", IP, "b.example.com", "198.51.100.8", "c.example.com", "198.51.100.9",
                     "d.example.com", "203.0.113.4")
    assert result.status == 0 and result.stderr == ""
    assert result.stdout == "a.example.com 198.51.100.7 123\nd.example.com 203.0.113.4 1250\n"
    assert parse_validation(result.stdout) == [Candidate("a.example.com", IP, 123), Candidate("d.example.com", "203.0.113.4", 1250)]
    assert curl_calls(result) == [
        curl("a.example.com"),
        curl("b.example.com", "198.51.100.8"),
        curl("c.example.com", "198.51.100.9"),
        curl("d.example.com", "203.0.113.4"),
    ]
    assert not result.stdin_leaked


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [("0.123456", 123), ("0.5", 500), ("0.000999", 0), ("0.0099", 9), ("1.2", 1200), ("3", 3000), ("12.34567", 12345), ("0.08", 80)],
)
def test_milliseconds_are_the_whole_milliseconds_of_the_connect_time(box, seconds, milliseconds):
    box.answer(curl("a.example.com"), f"2 {seconds}")
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.stdout == f"a.example.com {IP} {milliseconds}\n"


@pytest.mark.parametrize("output", ["", "2", "2 abc", "2 -0.1", "2 1e-3", "3 0.1", "garbage"])
def test_a_pair_with_unusable_curl_output_is_left_out(box, output):
    box.answer(curl("a.example.com"), output)
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 0 and result.stdout == ""


def test_exits_zero_when_nothing_passes(box):
    box.answer(curl("a.example.com"), "", status=28)
    result = box.run(SCRIPT, "a.example.com", IP, "b.example.com", "198.51.100.8")
    assert result.status == 0 and result.stdout == "" and len(curl_calls(result)) == 2


@pytest.mark.parametrize("args", [[], ["a.example.com"], ["a.example.com", IP, "b.example.com"]])
def test_arguments_that_are_not_pairs_are_an_error(box, args):
    result = box.run(SCRIPT, *args)
    assert result.status == 1 and result.stdout == ""
    assert "成对" in result.stderr
    assert curl_calls(result) == []


def test_a_pair_that_is_not_a_domain_and_an_address_is_skipped_without_calling_curl(box):
    box.answer(curl("good.example.com"), "2 0.1")
    result = box.run(SCRIPT, "bad domain", IP, "good.example.com", IP, "-x.example.com;", IP, "a.example.com", "not an ip")
    assert result.status == 0 and result.stdout == f"good.example.com {IP} 100\n"
    assert curl_calls(result) == [curl("good.example.com")]
    assert result.stderr.count("跳过") == 3


def test_stops_with_a_message_when_curl_is_missing(box):
    box.remove_tool("curl")
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 1 and "curl" in result.stderr and result.stdout == ""


def test_never_exits_255(box):
    box.answer(curl("a.example.com"), "", status=255)
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 0 and result.stdout == ""


# --- curl and its CA certificates must be there; a broken curl is not "no candidate" ------

APT = "apt-get -qq -o DPkg::Lock::Timeout=120"
PACKAGES = ("curl", "ca-certificates")


def installed(box, *packages: str) -> None:
    for package in packages:
        box.answer(f"dpkg-query -W -f=${{Status}} {package}", "install ok installed")


def test_installs_curl_and_ca_certificates_when_missing_before_the_first_check(box):
    box.answer(curl("a.example.com"), "2 0.1")
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 0 and result.stdout == f"a.example.com {IP} 100\n"
    first_curl = result.calls.index(curl("a.example.com"))
    assert result.calls[:first_curl] == [
        *[f"dpkg-query -W -f=${{Status}} {package}" for package in PACKAGES],
        f"{APT} update",
        f"{APT} -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y curl ca-certificates",
    ]


def test_installs_only_what_is_missing(box):
    installed(box, "curl")
    result = box.run(SCRIPT, "a.example.com", IP)
    assert f"{APT} -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install -y ca-certificates" in result.calls


def test_apt_is_left_alone_when_both_are_installed(box):
    installed(box, *PACKAGES)
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 0 and not any(call.startswith("apt-get") for call in result.calls)


def test_a_failed_install_stops_with_the_reason(box):
    box.answer(f"{APT} update", status=100)
    result = box.run(SCRIPT, "a.example.com", IP)
    assert result.status == 1 and result.stdout == ""
    assert "安装" in result.stderr and "curl" in result.stderr
    assert curl_calls(result) == []


@pytest.mark.parametrize("status", [2, 4, 127])
def test_a_curl_that_cannot_work_at_all_stops_instead_of_finding_no_candidate(box, status):
    installed(box, *PACKAGES)
    box.answer(curl("a.example.com"), "", status=status)
    result = box.run(SCRIPT, "a.example.com", IP, "b.example.com", "198.51.100.8")
    assert result.status == 1 and result.stdout == ""
    assert f"退出码 {status}" in result.stderr and "与候选无关" in result.stderr
    assert curl_calls(result) == [curl("a.example.com")]
