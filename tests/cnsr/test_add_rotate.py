"""``cnsr up`` step 8: the node's SNI rotation service, and the identity file it reads."""

from __future__ import annotations

import json
import re

import pytest
from deploy_fakes import run_up

from cnsr.errors import CnsrError
from cnsr.server.xray import BACKUPS_PATH, CONFIG_PATH, read_identity
from cnsr.ssh.remote import _read_asset

BIN = "/opt/cnsr/bin"
UNIT = "/etc/systemd/system/cnsr-rotate.service"
SCRIPTS = (
    "cnsr-rotate.sh", "rotate-control.sh", "rotate-switch.sh",
    "validate-candidates.sh", "verify-reality.sh", "china-check.sh",
)  # fmt: skip
IDENTITY = "/opt/cnsr/identity.env"
NODE_ENV = "/opt/cnsr/node.env"
ENABLE = "systemctl enable --now cnsr-rotate.service"
RESTART = "systemctl restart cnsr-rotate.service"
STOP = "systemctl stop cnsr-rotate.service"
SWITCHING = "/opt/cnsr/switching"
PENDING = "/opt/cnsr/line-pending.json"
IS_ACTIVE = 'sh -c systemctl is-active "$1" || true sh cnsr-rotate.service'
# cnsr-rotate.sh checks the alias with this before it uses it in a URL
DOMAIN_PATTERN = r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*"


def since(fake_deps, start: int) -> list[str]:
    return fake_deps.server.what()[start:]


def test_add_installs_and_starts_the_rotate_service(fake_deps, monkeypatch):
    server = fake_deps.server
    registered = []
    original = server._rotate_service

    def spy(verb):
        if verb == ["enable", "--now"]:
            registered.append(fake_deps.node())
        return original(verb)

    monkeypatch.setattr(server, "_rotate_service", spy)
    assert run_up(fake_deps, aws="jp-tokyo") == 0

    for name in SCRIPTS:
        path = f"{BIN}/{name}"
        assert (server.files[path], server.modes[path]) == (_read_asset(name), "755")
    assert (server.files[UNIT], server.modes[UNIT]) == (_read_asset("cnsr-rotate.service"), "644")
    unit = server.files[UNIT].splitlines()
    assert "SuccessExitStatus=143" in unit and "Restart=on-failure" in unit
    assert [line for line in unit if line.startswith("SuccessExitStatus=")] == ["SuccessExitStatus=143"]
    what = server.what()
    order = ["put /opt/cnsr/node.env", f"put {BIN}/cnsr-rotate.sh", f"put {UNIT}", "systemctl daemon-reload", ENABLE,
             IS_ACTIVE]
    assert [what.index(step) for step in order] == sorted(what.index(step) for step in order)
    assert RESTART not in what  # enable --now started it fresh
    assert server.service == "active"
    # the service starts only once the node is in KV with its line and token
    (node,) = registered
    assert node.line and node.node_token
    assert "[8/8] 启动换 SNI 服务：完成" in fake_deps.terminal.screen


def test_every_add_clears_the_stop_markers(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    markers = ("/opt/cnsr/stopped", "/opt/cnsr/rotate-failed", "/opt/cnsr/unauthorized-since", "/opt/cnsr/last-version")
    for marker in markers:
        server.files[marker] = "1700000000\n"
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    # a 401 spell or a version from before this add must not stop or pause the service later
    assert not any(marker in server.files for marker in markers)
    rerun = since(fake_deps, start)
    cleared = next(i for i, what in enumerate(rerun) if what.startswith("rm -f /opt/cnsr/stopped"))
    assert cleared < rerun.index(ENABLE)


def test_a_rerun_restarts_the_service_only_when_a_file_changed(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = since(fake_deps, start)
    assert not any(what.startswith(f"put {BIN}/") or what == f"put {UNIT}" for what in rerun)
    assert ENABLE in rerun and IS_ACTIVE in rerun and RESTART not in rerun

    for changed in (f"{BIN}/cnsr-rotate.sh", UNIT):
        server.files[changed] = "an older version\n"
        start = len(server.log)
        assert run_up(fake_deps, aws="jp-tokyo") == 0
        rerun = since(fake_deps, start)
        assert [what for what in rerun if what.startswith("put ") and ("/bin/" in what or "systemd" in what)] == [
            f"put {changed}"
        ]
        assert rerun.index(ENABLE) < rerun.index(RESTART) < rerun.index(IS_ACTIVE)
        assert server.files[changed] != "an older version\n"


@pytest.mark.parametrize("path", [NODE_ENV, IDENTITY])
def test_a_changed_env_file_restarts_the_service(fake_deps, path):
    # the service reads both files only when it starts
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    wanted = server.files[path]
    server.files[path] = wanted.replace("=", "=stale-", 1)
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = since(fake_deps, start)
    assert server.files[path] == wanted
    assert rerun.index(f"put {path}") < rerun.index(RESTART) and rerun.index(ENABLE) < rerun.index(RESTART)
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert RESTART not in since(fake_deps, start)


def test_an_env_change_cut_short_still_restarts_next_time(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    server.files[IDENTITY] = "ALIAS=jp-1\n"
    server.fail_on["systemctl restart"] = CnsrError("远程命令执行失败（退出码 1）。")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    del server.fail_on["systemctl restart"]
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert RESTART in since(fake_deps, start)


def test_a_restart_cut_short_is_done_by_the_next_run(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    server.files[f"{BIN}/verify-reality.sh"] = "an older version\n"
    server.fail_on["systemctl restart"] = CnsrError("远程命令执行失败（退出码 1）。")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    del server.fail_on["systemctl restart"]
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = since(fake_deps, start)
    assert not any(what.startswith(f"put {BIN}/") for what in rerun)  # the file is current by now
    assert RESTART in rerun
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert RESTART not in since(fake_deps, start)


def test_a_service_that_does_not_come_up_stops_the_add(fake_deps):
    fake_deps.server.service_fails = True
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    screen = fake_deps.terminal.screen
    assert "[8/8] 启动换 SNI 服务：失败" in screen
    assert "换 SNI 服务没有在节点上运行起来（状态：activating）" in screen and "journalctl -u cnsr-rotate" in screen
    assert 3 in fake_deps.sleeps  # it had a moment to fail before the check


def test_identity_env_is_root_only_and_holds_no_private_key(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    x = fake_deps.node().xray
    assert server.files[IDENTITY] == f"ALIAS=jp-1\nUUID={x.uuid}\nPUBLIC_KEY={x.public_key}\nSHORT_ID={x.short_id}\n"
    assert server.modes[IDENTITY] == "600"
    (private_key,) = server.generated
    assert private_key not in server.files[IDENTITY]
    assert read_identity(server.files[CONFIG_PATH])["private_key"] == private_key
    assert re.fullmatch(DOMAIN_PATTERN, "jp-1")
    what = server.what()
    assert what.index(f"put {IDENTITY}") < what.index(ENABLE)


def test_refilled_backups_clear_exhausted(fake_deps):
    fake_deps.server.latency = {f"site{i}.example.com": 50 - i for i in range(4)}  # three backups only
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    event = {"kind": "exhausted", "detail": "备选 SNI 已用完，保持旧配置", "at": 1700000000}
    state = {"version": 3, "sni": "site3.example.com", "request": None, "paused_since": None, "exhausted": True,
             "event": event}
    fake_deps.kv()["state:jp-1"] = json.dumps(state)
    fake_deps.server.latency = {f"site{i}.example.com": 20 + i for i in range(12)}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(json.loads(fake_deps.server.files[BACKUPS_PATH])) == 11  # every one that passed but the SNI in use
    assert json.loads(fake_deps.kv()["state:jp-1"]) == {**state, "exhausted": False}


def test_backups_that_were_not_refilled_leave_exhausted(fake_deps):
    fake_deps.server.latency = {f"site{i}.example.com": 50 - i for i in range(4)}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    state = json.dumps({"version": 1, "sni": "site3.example.com", "request": None, "exhausted": True})
    fake_deps.kv()["state:jp-1"] = state
    fake_deps.server.latency = {}  # nothing passes on the node
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.kv()["state:jp-1"] == state


def test_a_rerun_stops_the_service_before_it_touches_the_node(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    assert STOP not in server.what()  # nothing to stop on a first add
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = since(fake_deps, start)
    touches = [i for i, what in enumerate(rerun) if what in ("validate-candidates.sh", "install-docker.sh", "verify-reality.sh")
               or what.startswith(("put /opt/cnsr/backups.json", "put /opt/cnsr/xray", "put /opt/cnsr/identity.env"))]
    assert touches and rerun.index(STOP) < min(touches)
    assert rerun.index(STOP) < rerun.index(ENABLE) and server.service == "active"


def _switch_line(sni: str, dest_ip: str) -> str:
    return f'{{"sni":"{sni}","dest_ip":"{dest_ip}"}}'  # pending_line in cnsr-rotate.sh


def test_a_switch_left_unfinished_is_completed_when_its_config_is_in_place(fake_deps):
    # The service was killed mid-switch: undoing it later would put config.prev.json back over the
    # config this add just verified, so the switch is recorded as done for the service to report.
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server, x = fake_deps.server, fake_deps.node().xray
    line = _switch_line(x.sni, x.dest_ip)
    server.files[SWITCHING] = f'{line}\n{{"domain":"{x.sni}","ip":"{x.dest_ip}","latency_ms":40}}\n'
    start = len(server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = since(fake_deps, start)
    assert SWITCHING not in server.files and server.files[PENDING] == line + "\n" and server.modes[PENDING] == "600"
    assert rerun.index(f"put {PENDING}") < rerun.index(f"rm -f {SWITCHING}") < rerun.index(ENABLE)


def test_a_switch_left_before_its_config_went_in_is_dropped(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    server.files[SWITCHING] = _switch_line("other.example.com", "203.0.113.99") + "\n{}\n"
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert SWITCHING not in server.files and PENDING not in server.files


def test_a_pending_line_is_left_for_the_service(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    server.files[PENDING] = '{"sni":"a.example.com","dest_ip":"203.0.113.10"}\n'
    before = server.files[PENDING]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert server.files[PENDING] == before


START = "systemctl start cnsr-rotate.service"


@pytest.mark.parametrize("error", [CnsrError("握手验证失败"), KeyboardInterrupt()], ids=["error", "ctrl-c"])
def test_a_rerun_that_stops_part_way_starts_the_service_again(fake_deps, error):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    start = len(server.log)
    server.fail_on["verify-reality.sh"] = error
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_up(fake_deps, aws="jp-tokyo")
    else:
        assert run_up(fake_deps, aws="jp-tokyo") == 1
    rerun = since(fake_deps, start)
    assert rerun.index(STOP) < rerun.index(START) and server.service == "active"


def test_a_service_that_cannot_be_started_again_is_reported(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    fake_deps.server.fail_on[START] = CnsrError("远程命令执行失败（退出码 1）。")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    screen = fake_deps.terminal.screen
    assert "警告：换 SNI 服务没能重新启动" in screen and "重跑 cnsr up jp-1" in screen and "握手验证失败" in screen


def test_a_rerun_that_stops_before_the_service_was_stopped_leaves_it_alone(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    server = fake_deps.server
    start = len(server.log)
    server.fail_on["preflight.sh"] = CnsrError("体检失败")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    rerun = since(fake_deps, start)
    assert STOP not in rerun and START not in rerun and server.service == "active"


def test_a_second_ctrl_c_during_the_restart_still_warns(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.server.fail_on["verify-reality.sh"] = KeyboardInterrupt()
    fake_deps.server.fail_on[START] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_up(fake_deps, aws="jp-tokyo")
    assert "警告：换 SNI 服务没能重新启动" in fake_deps.terminal.screen
