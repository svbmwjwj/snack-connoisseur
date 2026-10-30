import json
import re

import httpx
import pytest

from cnsr import runlog
from cnsr.errors import CnsrError
from cnsr.hetzner.api import HetznerClient, HetznerError

TOKEN = "hz-secret-token-0123456789"


def ok(body, status=200):
    return httpx.Response(status, json=body)


def fail(status, code, message="detail"):
    return httpx.Response(status, json={"error": {"code": code, "message": message, "details": {}}})


class Clock:
    """A fake monotonic clock that only moves when the client sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(*responses, clock=None):
    """A client whose transport answers with the given responses in order and records requests."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    clock = clock or Clock()
    client = HetznerClient(TOKEN, transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)
    return client, seen


# --- requests ---


def test_request_sends_bearer_and_returns_the_body():
    hz, seen = make_client(ok({"server": {"id": 7}}))
    assert hz.request("GET", "/servers/7", params={"a": "b"}) == {"server": {"id": 7}}
    (req,) = seen
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert str(req.url) == "https://api.hetzner.cloud/v1/servers/7?a=b"


def test_request_sends_json_body():
    hz, seen = make_client(ok({}, status=201))
    hz.request("POST", "/ssh_keys", json={"name": "k"})
    assert json.loads(seen[0].content) == {"name": "k"}
    assert seen[0].headers["content-type"] == "application/json"


def test_request_returns_none_for_an_empty_body():
    hz, _ = make_client(httpx.Response(204))
    assert hz.request("DELETE", "/primary_ips/3") is None


def test_request_raises_on_non_json_success_body():
    hz, _ = make_client(httpx.Response(200, text="not json"))
    with pytest.raises(HetznerError) as info:
        hz.request("GET", "/servers")
    assert info.value.status == 200
    assert "响应不是有效的 JSON" in str(info.value)


def test_client_sets_a_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return ok({})

    HetznerClient(TOKEN, transport=httpx.MockTransport(handler)).request("GET", "/x")
    assert seen[0] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


# --- pagination ---


def page(key, items, next_page):
    return ok({key: items, "meta": {"pagination": {"page": 1, "per_page": 50, "next_page": next_page}}})


def test_pages_follows_next_page_and_merges_items():
    hz, seen = make_client(page("servers", [{"id": 1}, {"id": 2}], 2), page("servers", [{"id": 3}], None))
    assert hz.pages("/servers", "servers", params={"label_selector": "cnsr=jp-1"}) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [dict(r.url.params) for r in seen] == [
        {"label_selector": "cnsr=jp-1", "page": "1", "per_page": "50"},
        {"label_selector": "cnsr=jp-1", "page": "2", "per_page": "50"},
    ]


def test_pages_stops_without_pagination_meta():
    hz, seen = make_client(ok({"server_types": [{"id": 1}]}))
    assert hz.pages("/server_types", "server_types") == [{"id": 1}]
    assert len(seen) == 1


def test_pages_with_no_items():
    hz, _ = make_client(page("primary_ips", [], None))
    assert hz.pages("/primary_ips", "primary_ips") == []


def test_pages_raises_on_a_failed_page():
    hz, _ = make_client(page("servers", [{"id": 1}], 2), fail(503, "unavailable"))
    with pytest.raises(HetznerError) as info:
        hz.pages("/servers", "servers")
    assert info.value.status == 503


# --- errors ---


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "unauthorized", "token 无效"),
        (403, "forbidden", "没有写权限"),
        (404, "not_found", "资源不存在"),
        (409, "uniqueness_error", "重复"),
        (412, "resource_unavailable", "无货"),
        (422, "placement_error", "无货"),
        (429, "rate_limit_exceeded", "请求太频繁"),
        (423, "locked", "还有动作在进行"),
        (422, "server_not_stopped", "先关机"),
        (403, "resource_limit_exceeded", "上限"),
        (503, "unavailable", "稍后重试"),
    ],
)
def test_error_codes_become_chinese_reasons(status, code, expected):
    hz, _ = make_client(fail(status, code, f"english text for {code}"))
    with pytest.raises(HetznerError) as info:
        hz.request("POST", "/servers", json={})
    err = info.value
    assert (err.status, err.code, err.message) == (status, code, f"english text for {code}")
    assert str(err).startswith(f"Hetzner API 请求失败（HTTP {status}）：")
    assert expected in str(err)
    assert TOKEN not in str(err)


def test_fixed_reasons_keep_the_hetzner_text_out():
    hz, _ = make_client(fail(401, "unauthorized", "unable to authenticate"))
    with pytest.raises(HetznerError) as info:
        hz.request("GET", "/servers")
    assert "unable to authenticate" not in str(info.value)


def test_unknown_code_shows_code_and_message():
    hz, _ = make_client(fail(422, "invalid_input", "invalid input in field 'name'"))
    with pytest.raises(HetznerError) as info:
        hz.request("POST", "/servers", json={})
    assert "Hetzner 返回 invalid_input：invalid input in field 'name'" in str(info.value)


def test_error_without_json_body_still_raises_with_status():
    hz, _ = make_client(httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(HetznerError) as info:
        hz.request("GET", "/servers")
    assert (info.value.status, info.value.code) == (502, "")
    assert "HTTP 502" in str(info.value)


def test_hetzner_error_is_a_cnsr_error():
    hz, _ = make_client(fail(404, "not_found"))
    with pytest.raises(CnsrError):
        hz.request("GET", "/servers/1")


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(httpx.ConnectError("refused"), "网络连接失败"), (httpx.ReadTimeout("slow"), "请求超时")],
)
def test_transport_errors_have_status_zero(caplog, exc, reason):
    caplog.set_level("INFO", logger="cnsr")

    def boom(request):
        raise exc

    hz = HetznerClient(TOKEN, transport=httpx.MockTransport(boom))
    with pytest.raises(HetznerError) as info:
        hz.request("GET", "/servers")
    assert (info.value.status, info.value.code) == (0, "")
    assert str(info.value) == f"无法连接 Hetzner API：{reason}"
    assert re.search(rf"Hetzner GET /servers → {reason}，耗时 \d+\.\d 秒$", caplog.records[-1].getMessage())


# --- logging and secrets ---


def test_requests_are_logged_without_bodies_or_token(caplog):
    caplog.set_level("INFO", logger="cnsr")
    hz, _ = make_client(ok({"server": {}}, status=201), fail(403, "forbidden"))
    hz.request("POST", "/servers", json={"name": "body-7654321"})
    with pytest.raises(HetznerError):
        hz.request("DELETE", "/servers/9")
    lines = [r.getMessage() for r in caplog.records if r.name == "cnsr.hetzner.api"]
    assert len(lines) == 2
    assert re.search(r"^Hetzner POST /servers → HTTP 201，耗时 \d+\.\d 秒$", lines[0])
    assert re.search(r"^Hetzner DELETE /servers/9 → HTTP 403，耗时 \d+\.\d 秒$", lines[1])
    assert "body-7654321" not in caplog.text and TOKEN not in caplog.text


def test_token_is_registered_as_a_run_secret(tmp_path):
    with runlog.started(tmp_path, "up", None, ["up"]) as run:
        make_client()
        assert TOKEN in run.secrets


# --- actions ---


def action(status, error=None, command="start_server", action_id=5):
    return {"id": action_id, "command": command, "status": status, "progress": 100, "error": error}


def test_wait_action_returns_a_finished_action_without_polling():
    hz, seen = make_client()
    assert hz.wait_action(action("success"))["status"] == "success"
    assert seen == []


def test_wait_action_polls_until_success():
    clock = Clock()
    hz, seen = make_client(ok({"action": action("running")}), ok({"action": action("success")}), clock=clock)
    assert hz.wait_action(action("running"))["status"] == "success"
    assert [r.url.path for r in seen] == ["/v1/actions/5", "/v1/actions/5"]
    assert len(clock.slept) == 2


def test_wait_action_raises_when_the_action_fails():
    failed = action("error", {"code": "placement_error", "message": "error during placement"}, command="create_server")
    hz, _ = make_client(ok({"action": failed}))
    with pytest.raises(HetznerError) as info:
        hz.wait_action(action("running", command="create_server"))
    err = info.value
    assert (err.status, err.code, err.message) == (0, "placement_error", "error during placement")
    assert str(err).startswith("Hetzner 动作失败（create_server）：")
    assert "无货" in str(err)


def test_wait_action_raises_for_an_already_failed_action():
    hz, seen = make_client()
    with pytest.raises(HetznerError) as info:
        hz.wait_action(action("error", {"code": "action_failed", "message": "Action failed"}))
    assert "Hetzner 返回 action_failed：Action failed" in str(info.value)
    assert seen == []


def test_wait_action_gives_up_at_the_deadline():
    clock = Clock()
    hz, seen = make_client(*[ok({"action": action("running", command="poweroff_server")}) for _ in range(10)], clock=clock)
    with pytest.raises(CnsrError) as info:
        hz.wait_action(action("running", command="poweroff_server"), timeout=5)
    assert not isinstance(info.value, HetznerError)
    assert "等了 5 秒" in str(info.value) and "poweroff_server" in str(info.value)
    assert clock.now == 5 and sum(clock.slept) == 5
