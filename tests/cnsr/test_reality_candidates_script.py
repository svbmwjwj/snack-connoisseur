"""Tests for reality-candidates.sh, run against a fake server (see xray_script_harness)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from harden_harness import NODE
from xray_script_harness import box, harden_tool  # noqa: F401  (fixtures)

from cnsr.server.xray import XRAY_IMAGE

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash is not available on Windows")

SCRIPT = "reality-candidates.sh"
PRIVATE_KEY = "cHJpdmF0ZWtleXByaXZhdGVrZXlwcml2YXRla2V5cHI"
PUBLIC_KEY = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE"
CONFIG_DIR = "/usr/local/etc/xray-candidates"
PAIRS = ("a.example.com", "192.0.2.1", "b.example.com", "2001:db8::2", "c.example.com", "192.0.2.3")
KEYGEN = f"docker run --rm {XRAY_IMAGE} x25519"


def probe(n: int) -> str:
    return f"curl --socks5-hostname 127.0.0.1:{12000 + n} --max-time 10 -s -o /dev/null -w %{{http_code}} https://www.gstatic.com/generate_204"


def listen(port: int) -> str:
    return f"ss -Hltn sport = :{port}"


# Stands in for docker: logs the call; `x25519` prints docker-keys.txt; `run -d` copies the mounted
# config out (as server-config.json or client-config.json) before the script deletes it, and
# fails with docker-run.rc when that exists.
DOCKER = r"""#!/bin/bash
printf '%s\n' "docker $*" >> "$FAKE_DIR/calls.log"
[ -z "$(cat)" ] || : > "$FAKE_DIR/stdin-leak"
case "$*" in
    *" x25519") cat "$FAKE_DIR/docker-keys.txt"; exit 0 ;;
esac
case "$1" in
    run)
        for arg in "$@"; do
            case "$arg" in
                *:ro)
                    dir=${arg%%:*}
                    cp "$dir/config.json" "$FAKE_DIR/${dir##*/}-config.json"
                    ls -ld "$dir" "$dir/config.json" >> "$FAKE_DIR/workdir-listing"
                    printf '%s\n' "${dir%/*}" > "$FAKE_DIR/workdir-path"
                    ;;
            esac
        done
        [ ! -f "$FAKE_DIR/docker-run.rc" ] || exit "$(cat "$FAKE_DIR/docker-run.rc")"
        ;;
esac
exit 0
"""


@pytest.fixture
def node(box):
    """A node where both containers start and listen and every pair gets a 204."""
    (box.bin / "docker").unlink()
    (box.bin / "docker").write_text(DOCKER)
    (box.bin / "docker").chmod(0o755)
    (box.base / "docker-keys.txt").write_text(f"PrivateKey: {PRIVATE_KEY}\nPassword (PublicKey): {PUBLIC_KEY}\nHash32: x\n")
    for port in (11002, 12002):
        box.answer(listen(port), f"LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:*\n")
    for n in range(3):
        box.answer(probe(n), "204")
    return box


def check(box, *args: str):
    return box.run(SCRIPT, *(args or PAIRS))


def containers(result) -> list[str]:
    return [re.search(r"--name (\S+)", call).group(1) for call in result.calls if call.startswith("docker run -d")]


def removals(result) -> list[str]:
    return [call for call in result.calls if call.startswith("docker rm")]


def workdir(box) -> Path:
    return Path((box.base / "workdir-path").read_text().strip())


def config(box, which: str) -> dict:
    return json.loads((box.base / f"{which}-config.json").read_text())


def test_prints_the_pairs_that_get_a_204_in_order_and_starts_the_containers_as_specified(node):
    node.answer(probe(1), "000", status=28)
    result = check(node)
    assert result.status == 0 and result.stderr == ""
    assert result.stdout == "a.example.com 192.0.2.1\nc.example.com 192.0.2.3\n"
    server, client = containers(result)
    assert re.fullmatch(r"cnsr-candidates-server-[0-9]+-[0-9]+", server)
    assert re.fullmatch(r"cnsr-candidates-client-[0-9]+-[0-9]+", client)
    run = f"--user 0:0 --network host -v {{}}:{CONFIG_DIR}:ro {XRAY_IMAGE} run -c {CONFIG_DIR}/config.json"
    assert result.calls == [
        KEYGEN,
        f"docker run -d --name {server} " + run.format(f"{workdir(node)}/server"),
        f"docker run -d --name {client} " + run.format(f"{workdir(node)}/client"),
        listen(11002),
        listen(12002),
        probe(0),
        probe(1),
        probe(2),
        f"docker rm -f {server}",
        f"docker rm -f {client}",
    ]
    assert not result.stdin_leaked


def test_the_server_borrows_each_candidate_on_its_own_port(node):
    check(node)
    inbounds = config(node, "server")["inbounds"]
    assert [inbound["port"] for inbound in inbounds] == [11000, 11001, 11002]
    assert {inbound["listen"] for inbound in inbounds} == {"127.0.0.1"}
    reality = [inbound["streamSettings"]["realitySettings"] for inbound in inbounds]
    # An IPv6 target goes in brackets.
    assert [r["target"] for r in reality] == ["192.0.2.1:443", "[2001:db8::2]:443", "192.0.2.3:443"]
    assert [r["serverNames"] for r in reality] == [["a.example.com"], ["b.example.com"], ["c.example.com"]]
    assert {r["privateKey"] for r in reality} == {PRIVATE_KEY}
    (client,) = {c["id"] for inbound in inbounds for c in inbound["settings"]["clients"]}
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}", client)


def test_each_socks_port_reaches_its_own_candidate_with_vision_and_chrome(node):
    check(node)
    client = config(node, "client")
    assert [i["port"] for i in client["inbounds"]] == [12000, 12001, 12002]
    rules = {rule["inboundTag"][0]: rule["outboundTag"] for rule in client["routing"]["rules"]}
    outbounds = {o["tag"]: o for o in client["outbounds"]}
    for n, domain in enumerate(("a.example.com", "b.example.com", "c.example.com")):
        outbound = outbounds[rules[client["inbounds"][n]["tag"]]]
        (server,) = outbound["settings"]["vnext"]
        assert (server["address"], server["port"]) == ("127.0.0.1", 11000 + n)
        assert server["users"][0]["flow"] == "xtls-rprx-vision"
        settings = outbound["streamSettings"]["realitySettings"]
        assert (settings["serverName"], settings["fingerprint"], settings["publicKey"]) == (domain, "chrome", PUBLIC_KEY)


def test_the_config_directory_is_private_and_is_removed_afterwards(node):
    check(node)
    listing = (node.base / "workdir-listing").read_text().splitlines()
    assert [line[:10] for line in listing] == ["drwx------", "-rw-------"] * 2
    assert not workdir(node).exists()


def test_exits_zero_when_nothing_passes(node):
    for n in range(3):
        node.answer(probe(n), "000", status=28)
    result = check(node)
    assert result.status == 0 and result.stdout == ""
    assert len(removals(result)) == 2


def test_a_pair_that_is_not_a_domain_and_an_address_is_skipped(node):
    for port in (11000, 12000):  # one pair left: its ports are the last ones
        node.answer(listen(port), f"LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:*\n")
    result = check(node, "a.example.com", "192.0.2.1", 'x"y.example.com', "192.0.2.9", "z.example.com", "$(id)")
    assert result.status == 0 and result.stdout == "a.example.com 192.0.2.1\n"
    assert result.stderr.count("跳过无法识别的候选") == 2
    assert [i["port"] for i in config(node, "server")["inbounds"]] == [11000]


def test_nothing_usable_is_no_error_and_starts_nothing(node):
    result = check(node, "-x", "y")
    assert result.status == 0 and result.stdout == ""
    assert not any(call.startswith("docker") for call in result.calls)


@pytest.mark.parametrize("args", [(), ("a.example.com",), ("a.example.com", "192.0.2.1", "b.example.com")])
def test_arguments_that_are_not_pairs_stop_before_docker_is_touched(node, args):
    result = node.run(SCRIPT, *args)
    assert result.status == 1 and result.stdout == "" and result.stderr
    assert result.calls == []


def test_stops_when_docker_is_missing(node):
    node.remove_tool("docker")
    result = check(node)
    assert result.status == 2 and "docker" in result.stderr


def test_stops_when_the_keys_cannot_be_read(node):
    (node.base / "docker-keys.txt").write_text("something else\n")
    result = check(node)
    assert result.status == 2 and "密钥" in result.stderr
    assert containers(result) == []


def test_reads_the_older_key_layout_too(node):
    (node.base / "docker-keys.txt").write_text(f"Private key: {PRIVATE_KEY}\nPublic key: {PUBLIC_KEY}\n")
    assert check(node).stdout.count("\n") == 3


def test_a_container_that_cannot_start_stops_the_check_and_is_removed(node):
    (node.base / "docker-run.rc").write_text("125")
    result = check(node)
    assert result.status == 2 and "服务端容器" in result.stderr
    assert len(removals(result)) == 1 and not workdir(node).exists()
    assert not any(call.startswith("curl") for call in result.calls)


def test_stops_when_a_container_never_listens(node):
    node.answer(listen(12002), "")
    result = check(node)
    assert result.status == 2 and "客户端在 15 秒内没有开始监听" in result.stderr
    assert result.calls.count(listen(12002)) == 30
    assert not any(call.startswith("curl") for call in result.calls)
    assert len(removals(result)) == 2


def test_a_status_of_255_becomes_1(node, harden_tool):
    node.remove_tool("mktemp")
    (node.bin / "mktemp").symlink_to(harden_tool)
    node.answer("mktemp -d /tmp/cnsr-candidates.XXXXXX", status=255)
    result = check(node)
    assert result.status == 1


def test_the_script_uses_the_image_of_the_server():
    text = (NODE / SCRIPT).read_text()
    assert re.findall(r"^XRAY_IMAGE=(\S+)$", text, re.MULTILINE) == [XRAY_IMAGE]
