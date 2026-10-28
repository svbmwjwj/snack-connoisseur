"""One run log over a whole session of deployments and a removal holds no secret."""

from __future__ import annotations

import json

from deploy_fakes import AGENT, VPS_IP, FakeServer, agent_key, run_up, run_rm
from rm_harness import VPS_ANSWERS

from cnsr import runlog
from cnsr.state import META_KEY


def control_records(fakes) -> dict[str, dict]:
    """Every record in the control plane's KV namespace, parsed."""
    namespace_id = next(i for i, title in fakes.cloudflare.namespaces.items() if title == "cnsr-control")
    return {key: json.loads(raw) for key, raw in fakes.cloudflare.kv[namespace_id].items()}


def sensitive_values(fakes, servers: list[FakeServer]) -> dict[str, str]:
    """Every secret a run of these commands touches, by what it is; ``servers`` made the keys and passwords."""
    records = control_records(fakes)
    found = {
        "cloudflare token": "t-secret",
        "client token": records[META_KEY]["client_token"],
        "admin token": records[META_KEY]["admin_token"],
    }
    for key, node in records.items():
        if key.startswith("node:"):
            found[f"{node['alias']} node token"] = node["node_token"]
            found[f"{node['alias']} uuid"] = node["xray"]["uuid"]
    for number, private_key in enumerate([key for server in servers for key in server.generated], 1):
        found[f"private key {number}"] = private_key
    for number, fed in enumerate([fed for server in servers for fed in server.chpasswd], 1):
        found[f"console password {number}"] = fed.removeprefix("admin:").strip()
    return found


def test_a_full_run_log_holds_no_secret(fake_deps, tmp_path):
    fake_deps.agents[AGENT].append(agent_key("vps-1"))
    with runlog.started(tmp_path / "logs", "add", None, ["session"]) as run:
        assert run_up(fake_deps, "jp-1", aws="jp") == 0
        aws_server, vendor_server = fake_deps.server, FakeServer()  # a new server, so it makes its own keys
        vendor_server.latency, vendor_server.client_ip = aws_server.latency, aws_server.client_ip
        fake_deps.server = vendor_server
        fake_deps.terminal.inputs = list(VPS_ANSWERS)
        assert run_up(fake_deps, "vps-1", ip=VPS_IP) == 0
        sensitive = sensitive_values(fake_deps, [aws_server, vendor_server])
        fake_deps.terminal.inputs = ["jp-1"]
        assert run_rm(fake_deps, "jp-1") == 0
    assert run.path is not None and run.warning is None

    expected = {
        "cloudflare token", "client token", "admin token", "jp-1 node token", "jp-1 uuid",
        "vps-1 node token", "vps-1 uuid", "private key 1", "private key 2", "console password 1",
    }
    assert expected <= sensitive.keys()
    assert all(len(value) >= runlog.MIN_SECRET_LENGTH for value in sensitive.values())

    log = run.path.read_text(encoding="utf-8")
    leaked = [name for name, value in sensitive.items() if value in log]
    assert leaked == []
    assert "***" in log
    assert "[4/8] 扫描候选 SNI" in log
    assert any("docker" in line for line in log.splitlines())
