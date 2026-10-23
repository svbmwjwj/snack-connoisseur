"""Tests for verify-reality.sh, run against a fake server (see xray_script_harness)."""

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

SCRIPT = "verify-reality.sh"
UUID = "11111111-1111-4111-8111-111111111111"
PUBLIC_KEY = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE"
VALUES = {"UUID": UUID, "PUBLIC_KEY": PUBLIC_KEY, "SHORT_ID": "0123456789abcdef", "SNI": "www.example.com"}
SOCKS = "ss -Hltn sport = :10808"
PROBE = "curl --socks5-hostname 127.0.0.1:10808 --max-time 10 -s -o /dev/null -w %{http_code} https://www.gstatic.com/generate_204"
CONFIG_DIR = "/usr/local/etc/xray-verify"

# Stands in for docker: logs the call; on `run` it copies the mounted config and the listing of
# the mounted directory out before the script deletes them. docker-run.rc makes `run` fail and
# docker-logs.txt is what `logs` prints.
DOCKER = r"""#!/bin/bash
printf '%s\n' "docker $*" >> "$FAKE_DIR/calls.log"
[ -z "$(cat)" ] || : > "$FAKE_DIR/stdin-leak"
case "$1" in
    run)
        for arg in "$@"; do
            case "$arg" in
                *:ro)
                    dir=${arg%%:*}
                    cp "$dir/config.json" "$FAKE_DIR/client-config.json"
                    ls -ld "$dir" "$dir/config.json" > "$FAKE_DIR/workdir-listing"
                    printf '%s\n' "$dir" > "$FAKE_DIR/workdir-path"
                    ;;
            esac
        done
        [ ! -f "$FAKE_DIR/docker-run.rc" ] || exit "$(cat "$FAKE_DIR/docker-run.rc")"
        ;;
    logs)
        [ ! -f "$FAKE_DIR/docker-logs.txt" ] || cat "$FAKE_DIR/docker-logs.txt"
        ;;
esac
exit 0
"""


# Stands in for ss: silent for the first ss-silent-calls calls, then the socks port listens.
SLOW_SS = r"""#!/bin/bash
printf '%s\n' "ss $*" >> "$FAKE_DIR/calls.log"
count=0
[ ! -f "$FAKE_DIR/ss-count" ] || count=$(cat "$FAKE_DIR/ss-count")
echo $((count + 1)) > "$FAKE_DIR/ss-count"
[ "$count" -lt "$(cat "$FAKE_DIR/ss-silent-calls")" ] || echo "LISTEN 0 4096 127.0.0.1:10808 0.0.0.0:*"
"""


def env_file(box, **changes) -> Path:
    """The root-only file cnsr puts on the node for the script; a value of None leaves the key out."""
    values = {**VALUES, **changes}
    path = box.base / "verify.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items() if value is not None))
    path.chmod(0o600)
    return path


def verify(box, **changes):
    return box.run(SCRIPT, str(env_file(box, **changes)))


@pytest.fixture
def client(box):
    """A node where the client starts, listens and gets a 204; each test breaks one thing."""
    (box.bin / "docker").unlink()
    (box.bin / "docker").write_text(DOCKER)
    (box.bin / "docker").chmod(0o755)
    box.answer(SOCKS, "LISTEN 0 4096 127.0.0.1:10808 0.0.0.0:*\n")
    box.answer(PROBE, "204")
    return box


def removals(result) -> list[str]:
    return [call for call in result.calls if call.startswith("docker rm")]


def container_name(result) -> str:
    runs = [call for call in result.calls if call.startswith("docker run")]
    assert len(runs) == 1
    return re.search(r"--name (\S+)", runs[0]).group(1)


def workdir(box) -> Path:
    return Path((box.base / "workdir-path").read_text().strip())


def test_passes_on_http_204_and_starts_the_client_as_specified(client):
    result = verify(client)
    assert result.status == 0 and result.stdout == "REALITY 自检通过。\n" and result.stderr == ""
    name = container_name(result)
    assert re.fullmatch(r"cnsr-verify-[0-9]+-[0-9]+", name)
    mount = f"{workdir(client)}:{CONFIG_DIR}:ro"
    assert result.calls == [
        # root inside the client: the image runs as a non-root user, and the config is root-only
        f"docker run -d --name {name} --user 0:0 --network host -v {mount} {XRAY_IMAGE} run -c {CONFIG_DIR}/config.json",
        SOCKS,
        PROBE,
        f"docker rm -f {name}",
    ]
    assert not result.stdin_leaked


def test_the_client_config_is_vless_reality_with_vision_to_the_local_server(client):
    verify(client)
    config = json.loads((client.base / "client-config.json").read_text())
    assert config["inbounds"] == [
        {"tag": "socks", "listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}
    ]
    (outbound,) = config["outbounds"]
    assert outbound["protocol"] == "vless"
    assert outbound["settings"]["vnext"] == [
        {"address": "127.0.0.1", "port": 443, "users": [{"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}
    ]
    assert outbound["streamSettings"]["security"] == "reality"
    assert outbound["streamSettings"]["realitySettings"] == {
        "serverName": "www.example.com",
        "fingerprint": "chrome",
        "publicKey": PUBLIC_KEY,
        "shortId": "0123456789abcdef",
    }


def test_the_config_directory_is_private_and_is_removed_afterwards(client):
    verify(client)
    listing = (client.base / "workdir-listing").read_text().splitlines()
    assert [line[:10] for line in listing] == ["drwx------", "-rw-------"]
    assert not workdir(client).exists()


def test_fails_on_any_other_answer_and_still_cleans_up(client):
    client.answer(PROBE, "000", status=28)
    (client.base / "docker-logs.txt").write_text("[Warning] REALITY: invalid connection\n")
    result = verify(client)
    assert result.status == 1 and result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == "[Warning] REALITY: invalid connection"
    assert lines[-1] == "REALITY 自检失败：经 443 端口访问 gstatic 得到 000（应为 204）。"
    assert removals(result) == [f"docker rm -f {container_name(result)}"]
    assert not workdir(client).exists()


def test_a_200_is_not_a_pass(client):
    client.answer(PROBE, "200")
    result = verify(client)
    assert result.status == 1 and "得到 200" in result.stderr


def test_fails_when_the_socks_port_never_listens(client):
    client.answer(SOCKS, "")
    result = verify(client)
    assert result.status == 2  # a client that never listens is the node's fault, not the SNI's
    assert "15 秒内没有开始监听 10808 端口" in result.stderr.splitlines()[-1]
    assert result.calls.count(SOCKS) == 30 and result.calls.count("sleep 0.5") == 30
    assert not any(call.startswith("curl") for call in result.calls)
    assert len(removals(result)) == 1 and not workdir(client).exists()


def test_waits_until_the_socks_port_listens(client):
    (client.bin / "ss").unlink()
    (client.bin / "ss").write_text(SLOW_SS)
    (client.bin / "ss").chmod(0o755)
    (client.base / "ss-silent-calls").write_text("3")
    result = verify(client)
    assert result.status == 0
    assert result.calls.count(SOCKS) == 4 and result.calls.count("sleep 0.5") == 3


def test_fails_when_the_client_container_cannot_start_and_still_removes_it(client):
    (client.base / "docker-run.rc").write_text("125")
    result = verify(client)
    assert result.status == 2 and "客户端容器" in result.stderr  # 2: the check could not run
    assert len(removals(result)) == 1 and not workdir(client).exists()
    assert not any(call.startswith("curl") for call in result.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"SNI": None},
        {"UUID": "not-a-uuid"},
        {"PUBLIC_KEY": 'key"injection'},
        {"SHORT_ID": "xyz"},
        {"SHORT_ID": "0123456789abcdef0"},
        {"SNI": 'a"b.example.com'},
        {"SNI": "-x y"},
        {"UUID": "$(id)"},
    ],
)
def test_wrong_values_stop_before_docker_is_touched_and_the_file_is_deleted(client, changes):
    path = env_file(client, **changes)
    result = client.run(SCRIPT, str(path))
    assert result.status == 1 and result.stdout == "" and result.stderr
    assert result.calls == []
    assert not path.exists()


@pytest.mark.parametrize("args", [(), ("a", "b"), ("/nonexistent/verify.env",)])
def test_a_wrong_argument_stops_before_docker_is_touched(client, args):
    result = client.run(SCRIPT, *args)
    assert result.status == 1 and result.stdout == "" and result.stderr
    assert result.calls == []


def test_the_values_file_is_deleted_after_a_pass_and_after_a_failure(client):
    path = env_file(client)
    assert client.run(SCRIPT, str(path)).status == 0 and not path.exists()
    client.answer(PROBE, "000", status=28)
    path = env_file(client)
    assert client.run(SCRIPT, str(path)).status == 1 and not path.exists()


def test_the_values_file_is_read_as_text_not_run(client, tmp_path):
    marker = tmp_path / "executed"
    path = env_file(client, SNI=f"www.example.com\ntouch {marker}\nNAME=$(touch {marker})")
    assert client.run(SCRIPT, str(path)).status == 0  # the extra lines are ignored
    assert not marker.exists()


def test_values_with_windows_line_endings_are_read(client):
    path = env_file(client)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert client.run(SCRIPT, str(path)).status == 0


def test_stops_when_docker_is_missing(client):
    client.remove_tool("docker")
    result = verify(client)
    assert result.status == 2 and "docker" in result.stderr


def test_a_status_of_255_becomes_1(client, harden_tool):
    client.remove_tool("mktemp")
    (client.bin / "mktemp").symlink_to(harden_tool)
    client.answer("mktemp -d /tmp/cnsr-verify.XXXXXX", status=255)
    result = verify(client)
    assert result.status == 1
    assert not any(call.startswith("docker") for call in result.calls)


def test_the_script_uses_the_image_of_the_server():
    text = (NODE / SCRIPT).read_text()
    assert re.findall(r"^XRAY_IMAGE=(\S+)$", text, re.MULTILINE) == [XRAY_IMAGE]
