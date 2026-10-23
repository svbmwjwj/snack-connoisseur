import json
import re

import httpx
import pytest

from cnsr.cloudflare.api import CloudflareClient, CloudflareError
from cnsr.cloudflare.kv import KvStore
from cnsr.errors import CnsrError

TOKEN = "t-secret"
ZONE = "zone-1"
ACCOUNT = "acct-1"
NAMESPACE = "ns-1"
NAME = "edge-cache-v123.example.com"


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


def make_kv(*responses):
    cf, seen = make_client(*responses)
    return KvStore(cf, ACCOUNT, NAMESPACE), seen


# --- api client ---


def test_request_sends_bearer_and_unwraps_result():
    cf, seen = make_client(ok({"id": "x"}))
    assert cf.request("GET", "/zones/z1", params={"a": "b"}) == {"id": "x"}
    (req,) = seen
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.method == "GET"
    assert str(req.url) == "https://api.cloudflare.com/client/v4/zones/z1?a=b"


def test_request_sends_json_body():
    cf, seen = make_client(ok({}))
    cf.request("POST", "/x", json={"k": "v"})
    assert json.loads(seen[0].content) == {"k": "v"}
    assert seen[0].headers["content-type"] == "application/json"


def test_request_sends_multipart_files():
    cf, seen = make_client(ok({}))
    cf.request("PUT", "/x", files={"metadata": (None, '{"a": 1}', "application/json")})
    assert seen[0].headers["content-type"].startswith("multipart/form-data")
    assert b'name="metadata"' in seen[0].content
    assert b'{"a": 1}' in seen[0].content


def test_request_raises_on_failure():
    cf, _ = make_client(fail(403, "Authentication error"))
    with pytest.raises(CloudflareError) as info:
        cf.request("GET", "/zones")
    assert info.value.status == 403
    assert info.value.messages == ["Authentication error"]


def test_cloudflare_error_is_a_cnsr_error_with_readable_chinese_message():
    cf, _ = make_client(fail(403, "Authentication error", "Second problem"))
    with pytest.raises(CnsrError) as info:
        cf.request("GET", "/zones")
    text = str(info.value)
    assert "Cloudflare" in text and "403" in text
    assert "Authentication error" in text and "Second problem" in text
    assert TOKEN not in text


def test_request_failure_without_json_body_still_raises_with_status():
    cf, _ = make_client(httpx.Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(CloudflareError) as info:
        cf.request("GET", "/zones")
    assert info.value.status == 502
    assert info.value.messages == []
    assert "502" in str(info.value)
    assert "<html>" not in str(info.value)


def test_request_raises_when_envelope_reports_failure_on_2xx():
    cf, _ = make_client(httpx.Response(200, json={"success": False, "errors": [{"message": "nope"}], "result": None}))
    with pytest.raises(CloudflareError) as info:
        cf.request("GET", "/zones")
    assert info.value.messages == ["nope"]


def test_request_raises_on_non_json_success_body():
    cf, _ = make_client(httpx.Response(200, text="not json"))
    with pytest.raises(CloudflareError):
        cf.request("GET", "/zones")


def test_request_wraps_transport_errors(caplog):
    caplog.set_level("INFO", logger="cnsr")

    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    cf = CloudflareClient(TOKEN, transport=httpx.MockTransport(boom))
    with pytest.raises(CloudflareError) as info:
        cf.request("GET", "/zones")
    assert info.value.status == 0
    assert "无法连接" in str(info.value)
    assert TOKEN not in str(info.value)
    assert re.search(r"GET /zones → 网络连接失败，耗时 \d+\.\d 秒$", caplog.records[-1].getMessage())


def test_raw_returns_response_without_unwrapping():
    cf, _ = make_client(httpx.Response(200, text="plain value"))
    resp = cf.request("GET", "/x", raw=True)
    assert isinstance(resp, httpx.Response)
    assert resp.text == "plain value"


def test_raw_non_2xx_still_raises():
    cf, _ = make_client(fail(500, "boom"))
    with pytest.raises(CloudflareError) as info:
        cf.request("GET", "/x", raw=True)
    assert info.value.status == 500
    assert info.value.messages == ["boom"]


def test_client_sets_a_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return ok({})

    CloudflareClient(TOKEN, transport=httpx.MockTransport(handler)).request("GET", "/x")
    assert seen[0] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


def test_zone_returns_id_and_account_id():
    cf, seen = make_client(ok([{"id": ZONE, "name": "example.com", "status": "active", "account": {"id": ACCOUNT, "name": "n"}}]))
    assert cf.zone("example.com") == {"id": ZONE, "account": {"id": ACCOUNT}}
    assert seen[0].url.path == "/client/v4/zones"
    assert dict(seen[0].url.params) == {"name": "example.com"}


def test_zone_missing_raises_readable_error():
    cf, _ = make_client(ok([]))
    with pytest.raises(CnsrError) as info:
        cf.zone("example.com")
    assert "example.com" in str(info.value)


# --- kv ---


def test_kv_get_returns_raw_body():
    kv, seen = make_kv(httpx.Response(200, text='{"line": "x"}'))
    assert kv.get("node:a") == '{"line": "x"}'
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/client/v4/accounts/{ACCOUNT}/storage/kv/namespaces/{NAMESPACE}/values/node:a"


def test_kv_get_missing_returns_none():
    kv, _ = make_kv(fail(404, "get: 'key not found'"))
    assert kv.get("node:a") is None


def test_kv_get_other_errors_propagate():
    kv, _ = make_kv(fail(500, "boom"))
    with pytest.raises(CloudflareError):
        kv.get("node:a")


def test_kv_key_is_url_encoded():
    kv, seen = make_kv(httpx.Response(200, text="v"))
    kv.get("node:a/b c")
    assert seen[0].url.raw_path.decode().endswith("/values/node%3Aa%2Fb%20c")


def test_kv_get_preserves_unicode_value():
    kv, _ = make_kv(httpx.Response(200, content="线路 一".encode()))
    assert kv.get("k") == "线路 一"


def test_kv_put_sends_value_as_multipart():
    kv, seen = make_kv(ok(None))
    kv.put("meta:control", '{"host": "a.example.com"}')
    (req,) = seen
    assert req.method == "PUT"
    assert req.url.raw_path.decode().endswith("/values/meta%3Acontrol")
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="value"' in req.content
    assert b'{"host": "a.example.com"}' in req.content
    assert b'name="metadata"' in req.content


def test_kv_list_keys_follows_the_cursor():
    kv, seen = make_kv(
        ok([{"name": "node:a"}, {"name": "node:b"}], result_info={"count": 2, "cursor": "c1"}),
        ok([{"name": "node:c"}], result_info={"count": 1, "cursor": ""}),
    )
    assert kv.list_keys("node:") == ["node:a", "node:b", "node:c"]
    assert [r.url.path for r in seen] == [f"/client/v4/accounts/{ACCOUNT}/storage/kv/namespaces/{NAMESPACE}/keys"] * 2
    assert seen[0].url.params["prefix"] == "node:" and "cursor" not in seen[0].url.params
    assert seen[1].url.params["cursor"] == "c1"


def test_kv_delete_sends_delete():
    kv, seen = make_kv(ok(None))
    kv.delete("node:a")
    assert seen[0].method == "DELETE"
    assert seen[0].url.path.endswith("/values/node:a")


# --- the run log ---


def test_cloudflare_requests_are_logged_without_bodies(caplog):
    caplog.set_level("INFO", logger="cnsr")
    cf, _ = make_client(ok({"id": "x"}), fail(403, "denied"))
    cf.request("PUT", "/zones/z1/dns_records/r1", json={"value": "body-7654321"})
    with pytest.raises(CloudflareError):
        cf.request("GET", "/zones/z1")
    assert "PUT /zones/z1/dns_records/r1" in caplog.text and "200" in caplog.text and "403" in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.name == "cnsr.cloudflare.api"]
    assert len(lines) == 2 and all(re.search(r"HTTP \d{3}，耗时 \d+\.\d 秒$", line) for line in lines)
    assert "body-7654321" not in caplog.text and TOKEN not in caplog.text
