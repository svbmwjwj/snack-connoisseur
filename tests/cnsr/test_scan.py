"""Tests for the neighbour scan: address ranges, egress check, TLS probe and candidate filters."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time

import httpx
import pytest
from cryptography import x509
from tls_server import make_certificate, name

from cnsr.scan import egress, probe
from cnsr.scan.egress import Egress, is_direct
from cnsr.scan.filters import basic_ok, reputation_ok, shortlist
from cnsr.scan.probe import Hit, scan, sni_ok
from cnsr.scan.ranges import scan_network, scan_targets


def test_scan_targets_excludes_own_22():
    t = scan_targets("198.18.1.77")  # a whole /20 of test addresses: 198.18.0.0/15; own /22 is 198.18.0.0-3.255
    assert len(t) == 3071 and "198.18.1.77" not in t and "198.18.3.255" not in t
    assert "198.18.4.1" in t and "198.18.15.255" not in t
    assert t[0] == "198.18.4.0" and t[-1] == "198.18.15.254"
    assert [ipaddress.ip_address(a) for a in t] == sorted(ipaddress.ip_address(a) for a in t)


def test_scan_targets_in_the_middle_22_drops_both_20_edges():
    t = scan_targets("198.18.6.9")  # own /22 is 198.18.4.0 - 198.18.7.255
    assert len(t) == 3070
    assert "198.18.0.0" not in t and "198.18.15.255" not in t
    assert "198.18.0.1" in t and "198.18.15.254" in t
    assert "198.18.4.0" not in t and "198.18.7.255" not in t
    assert "198.18.3.255" in t and "198.18.8.0" in t


def test_scan_targets_in_the_last_22():
    t = scan_targets("198.18.14.9")  # own /22 is 198.18.12.0 - 198.18.15.255
    assert len(t) == 3071
    assert "198.18.0.0" not in t and "198.18.0.1" in t and "198.18.11.255" in t
    assert "198.18.12.0" not in t


# ---- egress -----------------------------------------------------------------------------


def echo_transport(answer, requests=None, fallback=None):
    """Answer the first domestic echo service with ``answer``, the fallback one with ``fallback``.

    An Exception is raised, an int is a status, None refuses the connection.
    """
    answers = {"4.ipw.cn": answer, "ipv4.ddnspod.com": fallback}

    def handler(request):
        if requests is not None:
            requests.append(request)
        if request.url.host not in answers:
            raise AssertionError(f"unexpected request: {request.url}")
        value = answers[request.url.host]
        if value is None:
            raise httpx.ConnectError("refused")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return httpx.Response(value, headers={"location": "https://elsewhere.example.com/"} if value == 302 else {})
        return httpx.Response(200, text=value)

    return httpx.MockTransport(handler)


def test_is_direct():
    assert is_direct(Egress("203.0.113.5", "203.0.113.5")) is True
    assert is_direct(Egress("203.0.113.5", "198.51.100.9")) is False
    assert is_direct(Egress("203.0.113.5", None)) is None
    assert is_direct(Egress(None, "203.0.113.5")) is None
    assert is_direct(Egress(None, None)) is None


def test_direct_address_reads_the_domestic_echo():
    requests = []
    assert egress.direct_address(echo_transport(" 203.0.113.5\n", requests)) == "203.0.113.5"
    assert [(r.method, r.url.host, r.url.path) for r in requests] == [("GET", "4.ipw.cn", "/")]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), 500, 302, "<html>blocked</html>", ""],
    ids=["refused", "timeout", "http-500", "redirect", "not-an-ip", "empty"],
)
def test_direct_address_is_none_when_the_echo_fails(failure):
    assert egress.direct_address(echo_transport(failure)) is None


def test_direct_address_falls_back_to_the_second_echo():
    requests = []
    assert egress.direct_address(echo_transport(httpx.ConnectError("reset"), requests, "203.0.113.5\n")) == "203.0.113.5"
    assert [r.url.host for r in requests] == ["4.ipw.cn", "ipv4.ddnspod.com"]


def test_direct_address_follows_no_redirect():
    requests = []
    egress.direct_address(echo_transport(302, requests, 302))
    assert len(requests) == 2


def test_direct_address_ignores_system_proxy_settings(monkeypatch):
    made = []
    real_client = httpx.Client

    def spy(**kwargs):
        made.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(egress.httpx, "Client", spy)
    egress.direct_address(echo_transport("203.0.113.5"))
    assert len(made) == 1
    assert made[0]["trust_env"] is False and made[0]["follow_redirects"] is False
    assert made[0]["timeout"] == 8.0


def fake_resolver(*answers):
    def resolve(host, port, **kwargs):
        if not answers:
            raise socket.gaierror("no name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in answers]

    return resolve


def test_fake_ip_dns_spots_a_proxy_that_answers_from_198_18():
    assert egress.fake_ip_dns(fake_resolver("198.18.12.63")) is True
    assert egress.fake_ip_dns(fake_resolver("198.19.255.1")) is True


def test_fake_ip_dns_is_false_for_real_answers_and_failures():
    assert egress.fake_ip_dns(fake_resolver("203.0.113.5")) is False
    assert egress.fake_ip_dns(fake_resolver("198.20.0.1")) is False
    assert egress.fake_ip_dns(fake_resolver()) is False


def test_fake_dns_means_not_direct_whatever_the_addresses():
    assert is_direct(Egress("203.0.113.5", "203.0.113.5", fake_dns=True)) is False
    assert is_direct(Egress(None, None, fake_dns=True)) is False


@pytest.mark.parametrize("text, address", [
    ("203.0.113.5 51234 22\n", "203.0.113.5"),
    ("2001:db8::5 51234 22", "2001:db8::5"),
    ("::ffff:203.0.113.5 51234 22", "203.0.113.5"),  # an IPv4 client on an IPv6 socket
    ("", None), ("\n", None), ("garbage 1 22", None),
])  # fmt: skip
def test_client_address_reads_ssh_client(text, address):
    assert egress.client_address(text) == address


def test_scan_network_is_the_nodes_20():
    assert scan_network("198.51.100.244") == "198.51.96.0/20"
    assert scan_network("198.18.1.77") == "198.18.0.0/20"


# ---- probe ------------------------------------------------------------------------------


def test_probe_reads_cert_domain(tls_server):  # TLS 1.3 + ALPN h2, self-signed cert, SAN probe.example.com
    hits = scan(["127.0.0.1"], port=tls_server.port, timeout=2)
    assert hits == [Hit("127.0.0.1", "probe.example.com", "probe.example.com", True)]


def test_probe_skips_without_h2(tls_server_no_alpn):
    assert scan(["127.0.0.1"], port=tls_server_no_alpn.port, timeout=2) == []


def test_probe_skips_tls12(tls_server_factory):
    server = tls_server_factory(tls_version=ssl.TLSVersion.TLSv1_2)
    assert scan(["127.0.0.1"], port=server.port, timeout=2) == []


def test_probe_sends_no_sni(tls_server):
    scan(["127.0.0.1"], port=tls_server.port, timeout=2)
    assert tls_server.server_names == [None]


def test_probe_with_nothing_listening_is_no_hit():
    assert scan(["127.0.0.1"], port=1, timeout=1) == []


def test_probe_gives_up_on_a_server_that_never_answers():
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(5)
        started = time.monotonic()
        assert scan(["127.0.0.1"], port=silent.getsockname()[1], timeout=0.3) == []
        assert time.monotonic() - started < 3


DNS = x509.DNSName
IP = x509.IPAddress


@pytest.mark.parametrize(
    "subject_cn, sans, expected",
    [
        ("probe.example.com", [DNS("*.example.com"), DNS("Www.Example.com"), DNS("b.example.com")], "www.example.com"),
        ("probe.example.com", [IP(ipaddress.ip_address("192.0.2.1")), DNS("192.0.2.9"), DNS("a.example.com")], "a.example.com"),
        ("cn.example.com", [], "cn.example.com"),
        ("CN.Example.com", [], "cn.example.com"),
        ("fallback.example.com", [DNS("*.example.com")], "fallback.example.com"),
        ("fallback.example.com", [IP(ipaddress.ip_address("192.0.2.1"))], "fallback.example.com"),
        ("localhost", [], "localhost"),
        ("*.example.com", [DNS("*.example.com")], None),
        ("*.example.com", [], None),
        ("Example Default Certificate", [], None),
        ("192.0.2.5", [], None),
        (None, [], None),
    ],
    ids=[
        "san-skips-wildcard-and-lowercases",
        "san-skips-ip-literals",
        "cn-when-no-san",
        "cn-lowercased",
        "cn-when-san-is-wildcard-only",
        "cn-when-san-is-ip-only",
        "single-label-name",
        "wildcards-only",
        "wildcard-cn-only",
        "cn-is-not-a-dns-name",
        "cn-is-an-ip",
        "nothing-at-all",
    ],
)
def test_probe_domain_choice(tls_server_factory, subject_cn, sans, expected):
    server = tls_server_factory(make_certificate(name(subject_cn, organization="Example Org"), sans=sans))
    hits = scan(["127.0.0.1"], port=server.port, timeout=2)
    assert (hits[0].domain if hits else None) == expected


def test_probe_issuer_is_the_issuer_common_name(tls_server_factory):
    certificate = make_certificate(
        name("www.example.com"), issuer=name("Example CA R1", organization="Example Org"), sans=[DNS("www.example.com")]
    )
    server = tls_server_factory(certificate)
    assert scan(["127.0.0.1"], port=server.port, timeout=2) == [
        Hit("127.0.0.1", "www.example.com", "Example CA R1", False)
    ]


def test_probe_issuer_without_common_name_is_the_rfc4514_string(tls_server_factory):
    certificate = make_certificate(
        name("www.example.com"), issuer=name(organization="Example Org"), sans=[DNS("www.example.com")]
    )
    server = tls_server_factory(certificate)
    assert scan(["127.0.0.1"], port=server.port, timeout=2) == [
        Hit("127.0.0.1", "www.example.com", "O=Example Org", False)
    ]


def test_probe_same_common_name_with_another_organization_is_not_self_signed(tls_server_factory):
    certificate = make_certificate(
        name("www.example.com"), issuer=name("www.example.com", organization="Example Org"), sans=[DNS("www.example.com")]
    )
    server = tls_server_factory(certificate)
    (hit,) = scan(["127.0.0.1"], port=server.port, timeout=2)
    assert hit.self_signed is False


def canned_handshake(monkeypatch, certificate, behaviour):
    """Replace the network handshake; ``behaviour(ip)`` returns a delay in seconds or raises."""
    state = {"running": 0, "peak": 0, "calls": []}

    async def fake(ip, port, server_hostname):
        state["calls"].append((ip, port, server_hostname))
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await asyncio.sleep(behaviour(ip))
        finally:
            state["running"] -= 1
        return "TLSv1.3", "h2", certificate.der

    monkeypatch.setattr(probe, "_handshake", fake)
    return state


def test_scan_returns_hits_in_input_order_and_bounds_concurrency(monkeypatch):
    certificate = make_certificate(name("www.example.com"), sans=[DNS("www.example.com")])
    # The first addresses answer last, so completion order is the reverse of input order.
    state = canned_handshake(monkeypatch, certificate, lambda ip: 0.06 - int(ip.rsplit(".", 1)[1]) * 0.0015)
    ips = [f"192.0.2.{n}" for n in range(1, 31)]
    hits = scan(ips, port=8443, concurrency=4, timeout=5)
    assert [h.ip for h in hits] == ips
    assert state["peak"] == 4
    assert {call[1:] for call in state["calls"]} == {(8443, "")}  # the port is passed through; no SNI


def test_scan_one_failing_address_does_not_stop_the_rest(monkeypatch):
    certificate = make_certificate(name("www.example.com"), sans=[DNS("www.example.com")])

    def behaviour(ip):
        if ip == "192.0.2.2":
            raise OSError("boom")
        if ip == "192.0.2.3":
            raise ValueError("odd certificate")
        return 0

    canned_handshake(monkeypatch, certificate, behaviour)
    hits = scan(["192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"])
    assert [h.ip for h in hits] == ["192.0.2.1", "192.0.2.4"]


def test_scan_timeout_covers_the_handshake(monkeypatch):
    certificate = make_certificate(name("www.example.com"), sans=[DNS("www.example.com")])
    canned_handshake(monkeypatch, certificate, lambda ip: 5 if ip == "192.0.2.2" else 0)
    started = time.monotonic()
    hits = scan(["192.0.2.1", "192.0.2.2"], timeout=0.2)
    assert [h.ip for h in hits] == ["192.0.2.1"]
    assert time.monotonic() - started < 3


def test_sni_ok(tls_server):
    assert sni_ok("127.0.0.1", "probe.example.com", port=tls_server.port, timeout=2)
    assert not sni_ok("127.0.0.1", "probe.example.com", port=1, timeout=1)


def test_sni_ok_sends_the_domain_as_sni(tls_server):
    assert sni_ok("127.0.0.1", "other.example.com", port=tls_server.port, timeout=2)
    assert tls_server.server_names == ["other.example.com"]


def test_sni_ok_needs_tls13_but_not_alpn(tls_server_no_alpn, tls_server_factory):
    assert sni_ok("127.0.0.1", "probe.example.com", port=tls_server_no_alpn.port, timeout=2)
    tls12 = tls_server_factory(tls_version=ssl.TLSVersion.TLSv1_2)
    assert not sni_ok("127.0.0.1", "probe.example.com", port=tls12.port, timeout=2)


def test_sni_ok_gives_up_on_a_server_that_never_answers():
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(5)
        started = time.monotonic()
        assert not sni_ok("127.0.0.1", "probe.example.com", port=silent.getsockname()[1], timeout=0.3)
        assert time.monotonic() - started < 3


# ---- filters ----------------------------------------------------------------------------


def hit(domain="www.example.com", issuer="Example CA R1", self_signed=False, ip="192.0.2.1"):
    return Hit(ip, domain, issuer, self_signed)


@pytest.mark.parametrize(
    "domain,ok",
    [
        ("panel.example.qzz.io", False),
        ("a.duckdns.org", False),
        ("stg-it.example.com", False),
        ("shop.example.top", False),
        ("gateway.icloud.com", False),
        ("devices.example.co.jp", True),
        ("www.example-corp.co.jp", True),
        # keywords match whole tokens split on "." and "-", any case
        ("DEV.example.com", False),
        ("api-test.example.com", False),
        ("my-demo-app.example.com", False),
        ("testing.example.com", True),
        ("staged.example.com", True),
        # low-reputation TLDs: only the last label counts
        ("www.example.xyz", False),
        ("shop.example.com", True),
        ("top.example.com", True),
        # dynamic DNS and big sites: suffix match on label boundaries
        ("duckdns.org", False),
        ("notduckdns.org", True),
        ("x.com", False),
        ("www.x.com", False),
        ("max.com", True),
        ("fakeicloud.com", True),
    ],
)
def test_reputation(domain, ok):
    assert reputation_ok(domain) is ok


@pytest.mark.parametrize(
    "changes,ok",
    [
        ({}, True),
        ({"self_signed": True}, False),
        ({"issuer": "CloudFlare Origin SSL Certificate Authority"}, False),
        ({"issuer": "cloudflare origin ecc ca"}, False),
        ({"issuer": "Cloudflare Inc ECC CA-3"}, True),
        ({"domain": "localhost"}, False),
        ({"domain": "LocalHost"}, False),
        ({"domain": "192.0.2.7"}, False),
        ({"domain": "2001:db8::1"}, False),
    ],
)
def test_basic_ok_rejects_self_signed_and_origin(changes, ok):
    assert basic_ok(hit(**changes)) is ok


def test_shortlist_dedupes_and_caps():
    good = [hit(f"www{i}.example.com", ip=f"192.0.2.{i + 1}") for i in range(40)]
    duplicates = [hit(f"www{i}.example.com", ip=f"198.51.100.{i + 1}") for i in range(0, 40, 3)]
    noise = [hit("bad.example.com", self_signed=True), hit("panel.example.com"), hit("localhost")]
    result = shortlist(noise + duplicates[:5] + good + duplicates[5:])
    assert len(result) == 30
    assert len({h.domain for h in result}) == 30
    assert all(basic_ok(h) and reputation_ok(h.domain) for h in result)
    assert shortlist(good[:5], limit=10) == good[:5] and shortlist([]) == []


def test_shortlist_keeps_the_first_occurrence_in_order():
    first = hit("a.example.com", ip="192.0.2.1")
    second = hit("b.example.com", ip="192.0.2.2")
    again = hit("a.example.com", ip="192.0.2.3")
    assert shortlist([first, second, again]) == [first, second]
    assert shortlist([again, second, first]) == [again, second]


def test_shortlist_filters_before_capping():
    hits = [hit("panel.example.com")] * 5 + [hit(f"www{i}.example.com") for i in range(4)]
    assert [h.domain for h in shortlist(hits, limit=3)] == ["www0.example.com", "www1.example.com", "www2.example.com"]
    assert shortlist([hit("panel.example.com"), hit("x.com"), hit("a.example.com", self_signed=True)]) == []
