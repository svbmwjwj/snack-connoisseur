"""Tests for the node DNS records and the resolution checks (cnsr.cloudflare.dns)."""

import json
import random
import socket

import httpx
import pytest

from cnsr.cloudflare import dns
from cnsr.cloudflare.api import CloudflareClient, CloudflareError
from cnsr.naming import random_label

TOKEN = "t-secret"
ZONE = "zone-1"
NAME = "edge-cache-v123.example.com"
NOTE = "cnsr node jp-1"


def ok(result, **extra):
    return httpx.Response(200, json={"success": True, "errors": [], "result": result, **extra})


def fail(status, *messages):
    return httpx.Response(status, json={"success": False, "errors": [{"message": m} for m in messages], "result": None})


def make_client(*responses):
    """A client whose transport answers with the given responses in order and records requests."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return CloudflareClient(TOKEN, transport=httpx.MockTransport(handler)), seen


# --- dns records ---


def test_upsert_creates_grey_record_with_ttl_60():
    cf, seen = make_client(ok([]), ok({"id": "rec-1"}))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    get, post = seen
    assert get.method == "GET"
    assert get.url.path == f"/client/v4/zones/{ZONE}/dns_records"
    assert get.url.query.decode() == f"type=A&name={NAME}"
    assert post.method == "POST"
    assert post.url.path == f"/client/v4/zones/{ZONE}/dns_records"
    assert json.loads(post.content) == {
        "type": "A",
        "name": NAME,
        "content": "192.0.2.10",
        "ttl": 60,
        "proxied": False,
        "comment": NOTE,
    }


def test_upsert_creates_aaaa_record():
    cf, seen = make_client(ok([]), ok({"id": "rec-6"}))
    assert dns.upsert_record(cf, ZONE, NAME, "AAAA", "2001:db8::10", comment=NOTE) == "rec-6"
    assert dict(seen[0].url.params)["type"] == "AAAA"
    body = json.loads(seen[1].content)
    assert body["type"] == "AAAA" and body["content"] == "2001:db8::10"
    assert body["proxied"] is False and body["ttl"] == 60


def grey(**fields) -> dict:
    """A record as cnsr creates it: grey cloud, TTL 60, with cnsr's comment."""
    return {"id": "rec-1", "proxied": False, "ttl": 60, "comment": NOTE, **fields}


def test_upsert_patches_changed_content():
    cf, seen = make_client(ok([grey(content="192.0.2.9")]), ok({"id": "rec-1"}))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    get, patch = seen
    assert get.method == "GET"
    assert patch.method == "PATCH"
    assert patch.url.path == f"/client/v4/zones/{ZONE}/dns_records/rec-1"
    assert json.loads(patch.content) == {"content": "192.0.2.10"}


def test_upsert_keeps_same_content_without_write():
    cf, seen = make_client(ok([grey(content="192.0.2.10")]))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.parametrize(
    ("record", "patch"),
    [
        (grey(content="192.0.2.10", proxied=True), {"proxied": False, "ttl": 60}),
        (grey(content="192.0.2.10", ttl=1), {"proxied": False, "ttl": 60}),
        (grey(content="192.0.2.9", proxied=True, ttl=300), {"content": "192.0.2.10", "proxied": False, "ttl": 60}),
        ({"id": "rec-1", "content": "192.0.2.10", "comment": NOTE}, {"proxied": False, "ttl": 60}),
    ],
)
def test_upsert_repairs_proxied_and_ttl(record, patch):
    cf, seen = make_client(ok([record]), ok({"id": "rec-1"}))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    assert seen[1].method == "PATCH" and json.loads(seen[1].content) == patch


@pytest.mark.parametrize("missing", [{"comment": None}, {"comment": ""}, {}])
def test_upsert_adds_the_comment_to_a_record_without_one(missing):
    record = {k: v for k, v in grey(content="192.0.2.10").items() if k != "comment"} | missing
    cf, seen = make_client(ok([record]), ok({"id": "rec-1"}))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    assert seen[1].method == "PATCH" and json.loads(seen[1].content) == {"comment": NOTE}


def test_upsert_keeps_a_comment_someone_else_wrote():
    cf, seen = make_client(ok([grey(content="192.0.2.10", comment="mine")]))
    assert dns.upsert_record(cf, ZONE, NAME, "A", "192.0.2.10", comment=NOTE) == "rec-1"
    assert [r.method for r in seen] == ["GET"]


def test_fresh_name_skips_names_with_records_of_any_type():
    cf, seen = make_client(ok([{"id": "x", "type": "MX"}]), ok([]))
    rng = random.Random(3)
    first, second = (f"{random_label(rng)}.example.com" for _ in range(2))
    assert dns.fresh_name(cf, ZONE, "example.com", random.Random(3)) == second
    assert [dict(r.url.params) for r in seen] == [{"name": first}, {"name": second}]


def test_delete_record_sends_delete():
    cf, seen = make_client(ok({"id": "rec-1"}))
    assert dns.delete_record(cf, ZONE, "rec-1") is None
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == f"/client/v4/zones/{ZONE}/dns_records/rec-1"


def test_delete_record_tolerates_404():
    cf, _ = make_client(fail(404, "Record does not exist"))
    assert dns.delete_record(cf, ZONE, "rec-1") is None


def test_delete_record_propagates_other_errors():
    cf, _ = make_client(fail(403, "Authentication error"))
    with pytest.raises(CloudflareError):
        dns.delete_record(cf, ZONE, "rec-1")


# --- dns resolution ---


def doh(*answers, status=0):
    body = {"Status": status}
    if answers:
        body["Answer"] = [{"name": NAME, "type": t, "TTL": 60, "data": d} for t, d in answers]
    return httpx.Response(200, json=body)


def doh_transport(response, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return response

    return httpx.MockTransport(handler)


def test_public_resolves_compares_answer_set():
    seen = []
    transport = doh_transport(doh((1, "192.0.2.10")), seen)
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=transport) is True
    (req,) = seen
    assert req.method == "GET"
    assert str(req.url.copy_with(query=None)) == "https://cloudflare-dns.com/dns-query"
    assert dict(req.url.params) == {"name": NAME, "type": "A"}
    assert req.headers["accept"] == "application/dns-json"
    assert dns.public_resolves(NAME, "A", {"192.0.2.9"}, transport=doh_transport(doh((1, "192.0.2.10")))) is False


def test_public_resolves_requires_the_exact_set():
    transport = doh_transport(doh((1, "192.0.2.10"), (1, "192.0.2.11")))
    assert dns.public_resolves(NAME, "A", {"192.0.2.10", "192.0.2.11"}, transport=transport) is True
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=transport) is False


def test_public_resolves_ignores_other_record_types():
    transport = doh_transport(doh((5, "other.example.com."), (1, "192.0.2.10")))
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=transport) is True


def test_public_resolves_aaaa_uses_type_28_and_normalizes_text():
    transport = doh_transport(doh((1, "192.0.2.10"), (28, "2001:db8::10")))
    assert dns.public_resolves(NAME, "AAAA", {"2001:0db8:0:0:0:0:0:10"}, transport=transport) is True
    assert dns.public_resolves(NAME, "AAAA", {"192.0.2.10"}, transport=transport) is False


def test_public_resolves_false_when_no_answer():
    transport = doh_transport(doh(status=3))
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=transport) is False


def test_public_resolves_false_when_lookup_fails():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=httpx.MockTransport(boom)) is False
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=doh_transport(httpx.Response(500))) is False
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=doh_transport(httpx.Response(200, text="x"))) is False


def resolver_answers(answers: dict[str, httpx.Response | Exception], seen: list) -> httpx.MockTransport:
    """Each public resolver (by host) answers with its response, or raises its exception."""

    def handler(request):
        seen.append(request)
        answer = answers[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler)


def test_public_resolves_falls_back_across_three_resolvers_in_order():
    seen = []
    answers = {
        "cloudflare-dns.com": httpx.ConnectError("blocked"),
        "1.1.1.1": httpx.Response(500),
        "dns.alidns.com": doh((28, "2001:db8::10")),
    }
    assert dns.public_resolves(NAME, "AAAA", {"2001:db8::10"}, transport=resolver_answers(answers, seen)) is True
    assert [str(r.url.copy_with(query=None)) for r in seen] == [
        "https://cloudflare-dns.com/dns-query", "https://1.1.1.1/dns-query", "https://dns.alidns.com/resolve",
    ]  # fmt: skip
    assert [r.headers.get("accept") for r in seen[:2]] == ["application/dns-json"] * 2
    assert dict(seen[1].url.params) == {"name": NAME, "type": "AAAA"}
    assert dict(seen[2].url.params) == {"name": NAME, "type": "28"}


def test_public_resolves_stops_at_the_first_resolver_with_the_expected_answer():
    seen = []
    answers = {"cloudflare-dns.com": doh((1, "192.0.2.9")), "1.1.1.1": doh((1, "192.0.2.10"))}
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=resolver_answers(answers, seen)) is True
    assert [r.url.host for r in seen] == ["cloudflare-dns.com", "1.1.1.1"]


def test_public_resolves_is_false_only_when_no_resolver_has_the_answer():
    seen = []
    answers = {"cloudflare-dns.com": doh((1, "192.0.2.9")), "1.1.1.1": httpx.ReadTimeout("slow"), "dns.alidns.com": doh(status=3)}
    assert dns.public_resolves(NAME, "A", {"192.0.2.10"}, transport=resolver_answers(answers, seen)) is False
    assert len(seen) == 3


def test_public_resolves_rejects_unsupported_type():
    with pytest.raises(ValueError):
        dns.public_resolves(NAME, "TXT", set(), transport=doh_transport(doh()))


def fake_getaddrinfo(addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        assert host == NAME
        out = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    return getaddrinfo


@pytest.mark.parametrize(
    ("resolved", "expected", "result"),
    [
        (["192.0.2.10", "2001:db8::10"], {"192.0.2.10", "2001:db8::10"}, True),
        (["192.0.2.10"], {"192.0.2.10", "2001:db8::10"}, True),  # AAAA filtered by the local resolver
        (["192.0.2.10", "2001:db8::10"], {"192.0.2.10"}, False),  # an address that is not the node's
        (["2001:db8::10"], {"192.0.2.10", "2001:db8::10"}, False),  # the IPv4 address is missing
        (["192.0.2.10", "192.0.2.11"], {"192.0.2.10"}, False),
        (["192.0.2.11"], {"192.0.2.10"}, False),
    ],
)
def test_local_resolves_needs_the_ipv4_and_only_the_nodes_addresses(monkeypatch, resolved, expected, result):
    monkeypatch.setattr(dns.socket, "getaddrinfo", fake_getaddrinfo(resolved))
    assert dns.local_resolves(NAME, expected) is result


def test_local_resolves_false_when_name_does_not_resolve(monkeypatch):
    def failing(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(dns.socket, "getaddrinfo", failing)
    assert dns.local_resolves(NAME, {"192.0.2.10"}) is False
