"""Tests for ``cnsr up``: the first run, the pipeline order and the review-focus guarantees."""

from __future__ import annotations

import functools
import json
import re

from deploy_fakes import VPS_IP, preflight_output, run_up

from cnsr import localconf
from cnsr.deploy import steps
from cnsr.qx import client_lines
from cnsr.scan.egress import Egress
from cnsr.server.xray import CONFIG_PATH, NODE_DIR, XRAY_IMAGE, read_identity
from cnsr.ssh.config import BACKUP_NAME
from cnsr.state import ControlMeta

# user (default root), port (default 22), then "已保存" once the console password is shown
VPS_ANSWERS = ["", "", "已保存"]
STEP_NAMES = ["step_ip", "step_connect", "step_harden", "step_scan", "step_sni", "step_xray", "step_register",
              "step_rotate"]


def test_noninteractive_missing_alias_fails(fake_deps):
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, alias=None, aws="jp-tokyo") == 1
    assert "缺少参数" in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == []


def test_first_run_saves_cloudflare_settings(fake_deps):
    fake_deps.deps.conf_path.unlink()
    fake_deps.terminal.inputs = ["t-new", "example.com"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    saved = localconf.load(fake_deps.deps.conf_path)
    assert (saved["CLOUDFLARE_API_TOKEN"], saved["CLOUDFLARE_ZONE"]) == ("t-new", "example.com")
    assert fake_deps.calls["make_cf"] == [("t-new",)]


def test_missing_token_permissions_stop_without_saving(fake_deps):
    fake_deps.deps.conf_path.unlink()
    fake_deps.cloudflare.deny("GET", "/accounts/acct-1/workers/scripts")
    fake_deps.terminal.inputs = ["t-new", "example.com"]
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "编辑 Workers 脚本" in fake_deps.terminal.text
    assert not fake_deps.deps.conf_path.exists()
    assert fake_deps.cloudflare.scripts == {}


def test_pipeline_runs_steps_in_order(fake_deps, monkeypatch):
    order = []

    def recording(step):
        @functools.wraps(step)
        def wrapper(ctx):
            order.append(step.__name__)
            step(ctx)

        return wrapper

    assert [step.__name__ for step in steps.STEPS] == STEP_NAMES
    monkeypatch.setattr(steps, "STEPS", tuple(recording(step) for step in steps.STEPS))
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert order == STEP_NAMES
    headers = re.findall(r"^\[(\d)/(\d)\] [^：]*$", fake_deps.terminal.screen, flags=re.M)
    assert headers == [(str(i), "8") for i in range(1, 9)]
    what = fake_deps.server.what()
    milestones = ["preflight.sh", "baseline-check.sh", "validate-candidates.sh", "install-docker.sh",
                  "verify-reality.sh", "put /opt/cnsr/node.env"]
    assert [what.index(m) for m in milestones] == sorted(what.index(m) for m in milestones)


def test_preflight_findings_stop_before_any_change(fake_deps):
    # preflight output contains listen=0.0.0.0:443 -> returns 1 with "443 端口已被占用";
    # no DNS upsert, no harden script, no scan, no docker install were called
    # (also: ~/.ssh/config and its backup untouched, no node record kept)
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    original = f"Host jp-1\n    HostName {VPS_IP}\n    User ops\n\nHost other\n    Port 2200\n".encode()
    ssh_config.write_bytes(original)  # the alias names this server, so add logs in as it says
    backup = ssh_config.parent / BACKUP_NAME
    backup.write_bytes(b"# older backup\n")
    fake_deps.server.preflight = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"))
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert "443 端口已被占用" in fake_deps.terminal.text
    assert {method for method, _ in fake_deps.dns.calls()} <= {"GET"} and fake_deps.dns.records == {}
    server = fake_deps.server
    assert not server.did("harden-stage.sh") and not server.did("install-docker.sh")
    assert fake_deps.calls["scan"] == []
    assert server.files == {} and NODE_DIR not in server.dirs
    assert ssh_config.read_bytes() == original
    assert backup.read_bytes() == b"# older backup\n"
    assert fake_deps.node() is None


def test_hostkey_mismatch_prompts_then_forgets(fake_deps):
    # first connect raises SshError(kind="hostkey"); ui answers 1 (删除旧指纹并重试) -> forget_host_key called once, then preflight runs
    fake_deps.server.hostkey_stale = True
    fake_deps.terminal.inputs = ["", "", "1", "已保存"]
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert "1. 删除旧指纹并重试" in fake_deps.terminal.text
    assert fake_deps.calls["forget"] == [(VPS_IP, 22)]
    what = fake_deps.server.what()
    assert what.index("ssh-keygen -R") < what.index("preflight.sh")


def test_hostkey_mismatch_declined_stops(fake_deps):
    fake_deps.server.hostkey_stale = True
    fake_deps.terminal.inputs = ["", "", "2"]
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert "主机指纹不符" in fake_deps.terminal.text
    assert fake_deps.calls["forget"] == []
    assert not fake_deps.server.did("preflight.sh")


def test_scan_stops_when_the_route_to_the_node_is_not_direct(fake_deps):
    fake_deps.egress_result = Egress("203.0.113.5", "198.51.100.9")  # the node sees a proxy's address
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "198.51.96.0/20" in fake_deps.terminal.text and "关闭代理" in fake_deps.terminal.text
    assert fake_deps.calls["scan"] == [] and fake_deps.calls["sni_ok"] == []
    assert not fake_deps.server.did("validate-candidates.sh")


def test_rerun_preserves_identity(fake_deps):
    # inventory already has node with xray; remote config.json exists -> no x25519 call; resulting node.xray.uuid unchanged
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    first = fake_deps.node()
    assert first.xray is not None and CONFIG_PATH in fake_deps.server.files
    logged = len(fake_deps.server.log)

    assert run_up(fake_deps, aws="jp-tokyo") == 0
    rerun = fake_deps.server.what()[logged:]
    assert not any("x25519" in what for what in rerun)
    (private_key,) = fake_deps.server.generated
    node = fake_deps.node()
    assert node.xray == first.xray and node.node_token == first.node_token and node.domain == first.domain
    identity = read_identity(fake_deps.server.files[CONFIG_PATH])
    assert (identity["uuid"], identity["private_key"], identity["sni"]) == (first.xray.uuid, private_key, first.xray.sni)
    assert len(fake_deps.calls["scan"]) == 1  # ten backups on file: no second scan
    assert fake_deps.lightsail.names().count("ensure_instance") == 2  # reused, never a second create


def test_success_prints_client_lines(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    text = fake_deps.terminal.text
    node = fake_deps.node()
    meta_raw = next(kv["meta:control"] for kv in fake_deps.cloudflare.kv.values() if "meta:control" in kv)
    assert "[server_remote]" in text and node.line in text
    assert client_lines(ControlMeta.from_json(meta_raw)) in text
    meta = ControlMeta.from_json(meta_raw)
    assert f"https://{meta.host}/{meta.client_token}/v2rayn" in text
    assert "resource-tag-regex" in text
    assert node.line.startswith(f"vless={node.domain}:443, ") and node.line.endswith(", tag=jp-1")
    assert XRAY_IMAGE not in fake_deps.terminal.screen  # the remote command lines in the log do name it


def test_add_puts_the_node_in_the_index_and_keeps_older_nodes(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    kv = fake_deps.kv()
    assert json.loads(kv["index:nodes"]) == ["jp-1"]
    # A control plane written before the index: its nodes are found by listing, once.
    del kv["index:nodes"]
    kv["node:old-1"] = '{"alias": "old-1", "line": "vless=198.51.100.1:443"}'
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert json.loads(kv["index:nodes"]) == ["jp-1", "old-1"]


def test_a_revoked_saved_token_says_how_to_enter_a_new_one(fake_deps):
    fake_deps.cloudflare.deny("GET", "/zones", 401)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    text = fake_deps.terminal.text
    assert str(fake_deps.deps.conf_path) in text and "删除 CLOUDFLARE_API_TOKEN" in text and "重跑" in text
    assert fake_deps.terminal.prompts == [] and fake_deps.lightsail.calls == []


def test_an_error_goes_to_stderr(fake_deps):
    assert run_up(fake_deps, alias="Bad Alias", aws="jp-tokyo") == 1
    assert fake_deps.terminal.err.getvalue().startswith("错误：别名只能包含")
    assert "错误" not in fake_deps.terminal.out.getvalue()
