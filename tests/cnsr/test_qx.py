"""Tests for the Quantumult X lines printed after a deployment."""

from __future__ import annotations

from cnsr.qx import client_lines, node_line
from cnsr.state import ControlMeta, XrayInfo


def test_node_line_exact():
    x = XrayInfo("11111111-1111-4111-8111-111111111111", "PUB", "0123456789abcdef", "www.example.com", "198.51.100.7")
    assert node_line("edge-cache-v123.example.com", x, "jp-1") == (
        "vless=edge-cache-v123.example.com:443, method=none, password=11111111-1111-4111-8111-111111111111, "
        "obfs=over-tls, obfs-host=www.example.com, reality-base64-pubkey=PUB, reality-hex-shortid=0123456789abcdef, "
        "vless-flow=xtls-rprx-vision, udp-relay=true, tag=jp-1")


def test_node_line_never_enables_fast_open():
    x = XrayInfo("u", "k", "s", "www.example.com", "198.51.100.7")
    assert "fast-open" not in node_line("n.example.com", x, "jp-1")


def test_client_lines():
    meta = ControlMeta(host="c.example.com", zone="example.com", client_token="ct", admin_token="at")
    assert client_lines(meta) == (
        "[server_remote]\n"
        "https://c.example.com/ct/cnsr.snippet, tag=cnsr, update-interval=86400, opt-parser=false, enabled=true\n"
        "\n"
        "[filter_local]\n"
        "host, c.example.com, direct\n"
        "\n"
        "[task_local]\n"
        "* * * * * https://c.example.com/ct/check.js, tag=cnsr 节点检测, enabled=true\n"
        "event-interaction https://c.example.com/ct/check-ui.js, tag=cnsr 检测, enabled=true"
    )


def test_client_lines_include_both_check_scripts():
    meta = ControlMeta(host="c.example.com", zone="example.com", client_token="ct", admin_token="at")
    lines = client_lines(meta).splitlines()
    tasks = lines[lines.index("[task_local]") + 1 :]
    assert tasks == [
        "* * * * * https://c.example.com/ct/check.js, tag=cnsr 节点检测, enabled=true",
        "event-interaction https://c.example.com/ct/check-ui.js, tag=cnsr 检测, enabled=true",
    ]


def test_client_lines_never_show_the_admin_token():
    meta = ControlMeta(host="c.example.com", zone="example.com", client_token="ct", admin_token="admin-secret")
    assert "admin-secret" not in client_lines(meta)
