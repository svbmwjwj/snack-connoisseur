"""Tests for probing a node's port from outside through the control plane (control.probe)."""

import json
import re

import httpx
import pytest

from cnsr import __version__
from cnsr.cloudflare import control
from cnsr.cloudflare.control import ProbeResult
from cnsr.errors import CnsrError
from cnsr.state import ControlMeta

HAS_CHINESE = re.compile(r"[一-鿿]")

META = ControlMeta(host="c.example.com", zone="example.com", client_token="ct", admin_token="at")


def probe_with(response, seen=None, **kwargs):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return response

    return control.probe(META, kwargs.pop("host", "2001:db8::10"), transport=httpx.MockTransport(handler), **kwargs)


def test_probe_posts_bearer():
    seen = []
    result = probe_with(httpx.Response(200, json={"ok": True, "ms": 42, "error": None}), seen)
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://c.example.com/admin/probe"
    assert req.headers["authorization"] == "Bearer at"
    assert json.loads(req.content) == {"host": "2001:db8::10", "port": 443}
    assert result == ProbeResult(ok=True, ms=42, error=None)
    assert result.control_reached is True


def test_probe_passes_a_custom_port():
    seen = []
    probe_with(httpx.Response(200, json={"ok": True, "ms": 1, "error": None}), seen, host="192.0.2.10", port=8443)
    assert json.loads(seen[0].content) == {"host": "192.0.2.10", "port": 8443}


def test_probe_reports_a_timeout_from_the_worker_in_chinese():
    result = probe_with(httpx.Response(200, json={"ok": False, "ms": None, "error": "timeout"}))
    assert result.ok is False and result.ms is None
    assert result.error == "连接超时"


@pytest.mark.parametrize(
    ("worker_error", "text"),
    [
        ("Connection refused", "连接被拒绝"),
        ("connect(): ECONNREFUSED", "连接被拒绝"),
        ("Network connection lost.", "连接失败"),
        ("proxy request failed, cannot connect to the specified address", "连接失败"),
    ],
)
def test_probe_maps_worker_errors_to_fixed_chinese_texts(worker_error, text):
    result = probe_with(httpx.Response(200, json={"ok": False, "ms": None, "error": worker_error}))
    assert result.ok is False and result.ms is None
    assert result.error == text and result.control_reached is True


def test_probe_failure_without_a_message_still_has_chinese_text():
    result = probe_with(httpx.Response(200, json={"ok": False, "ms": None, "error": None}))
    assert result.ok is False and HAS_CHINESE.search(result.error)


@pytest.mark.parametrize("status", [400, 401, 404, 405, 500, 503, 522])
def test_probe_maps_non_200_statuses_to_chinese_errors(status):
    with pytest.raises(CnsrError) as info:
        probe_with(httpx.Response(status, text="Unauthorized: bad bearer secret-admin-token"))
    text = str(info.value)
    assert HAS_CHINESE.search(text)
    assert str(status) in text
    assert "Unauthorized" not in text and "secret-admin-token" not in text


def test_probe_status_messages_differ_by_cause():
    messages = set()
    for status in (400, 401, 404, 503):
        with pytest.raises(CnsrError) as info:
            probe_with(httpx.Response(status, text="x"))
        messages.add(str(info.value))
    assert len(messages) == 4


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"ok": "yes", "ms": null, "error": null}', b'{"ms": 3}'])
def test_probe_rejects_an_unrecognised_200_body(body):
    with pytest.raises(CnsrError) as info:
        probe_with(httpx.Response(200, content=body))
    assert HAS_CHINESE.search(str(info.value))


@pytest.mark.parametrize(
    "exc,hint",
    [
        (httpx.ConnectError("refused"), "无法连接"),
        (httpx.ConnectTimeout("slow"), "超时"),
        (httpx.ReadTimeout("slow"), "超时"),
    ],
)
def test_probe_returns_a_failed_result_on_transport_errors(exc, hint):
    def boom(request):
        raise exc

    result = control.probe(META, "192.0.2.10", transport=httpx.MockTransport(boom))
    assert result.ok is False and result.ms is None
    assert hint in result.error
    assert result.control_reached is False  # the node was never tried


def test_probe_uses_a_short_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True, "ms": 1, "error": None})

    control.probe(META, "192.0.2.10", transport=httpx.MockTransport(handler))
    assert seen[0] == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


# --- a Cloudflare challenge in front of the control plane ----------------------------------

CHALLENGE = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<title>Just a moment...</title>")


def test_probe_names_a_cloudflare_challenge():
    with pytest.raises(control.ControlPlaneBlocked) as caught:
        probe_with(CHALLENGE)
    message = str(caught.value)
    assert "c.example.com" in message and "安全性" in message and "事件" in message


def reachable_with(response=None, error=None):
    def handler(request):
        assert (request.method, str(request.url)) == ("GET", "https://c.example.com/")
        if error is not None:
            raise error
        return response

    control.check_reachable(META, transport=httpx.MockTransport(handler))


def test_a_challenged_control_plane_is_not_reachable():
    with pytest.raises(control.ControlPlaneBlocked):
        reachable_with(CHALLENGE)


@pytest.mark.parametrize("response", [httpx.Response(404, text="not found"), httpx.Response(200, text="ok")])
def test_any_answer_from_the_worker_itself_is_reachable(response):
    reachable_with(response)


def test_a_control_plane_still_waiting_for_its_certificate_is_left_to_the_probe():
    reachable_with(error=httpx.ConnectError("certificate not ready"))


def test_requests_to_the_control_plane_name_cnsr_as_the_client():
    # Cloudflare's browser integrity check challenges python-httpx's default user agent.
    seen = []
    probe_with(httpx.Response(200, json={"ok": True, "ms": 1, "error": None}), seen)

    def handler(request):
        seen.append(request)
        return httpx.Response(404)

    control.check_reachable(META, transport=httpx.MockTransport(handler))
    assert [request.headers["user-agent"] for request in seen] == [f"cnsr/{__version__}"] * 2
