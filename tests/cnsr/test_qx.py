"""Tests for the Quantumult X lines printed after a deployment."""

from __future__ import annotations

from cnsr.qx import node_line
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

