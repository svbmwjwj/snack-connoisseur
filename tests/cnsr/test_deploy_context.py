"""Tests for the deployment context: the real defaults, credentials, saving and file checks."""

from __future__ import annotations

import secrets
from pathlib import Path

from deploy_fakes import AGENT, VPS_IP, agent_key
from ssh_fakes import FakeSsh

from cnsr.deploy.context import (
    AddRequest,
    DeployContext,
    _aws_default_credentials,
    _detect_agents,
    _make_lightsail,
    default_deps,
)
from cnsr.ssh import agent
from cnsr.ssh.agent import SESSION_AGENT, known_sockets
from cnsr.ssh.remote import Remote, Target
from cnsr.server.xray import PORTS
from cnsr.state import ControlMeta, Node, SshInfo
from cnsr.ui import Ui

META = ControlMeta(host="c.example.com", zone="example.com", client_token="ct", admin_token="at")


class RecordingInventory:
    def __init__(self) -> None:
        self.puts: list[str] = []

    def put(self, node: Node) -> None:
        self.puts.append(node.to_json())


def context(deps, inventory=None) -> DeployContext:
    node = Node(alias="jp-1", source="vps", ipv4=VPS_IP, ssh=SshInfo("root", 22, "jp-1"))
    req = AddRequest("jp-1", "vps", None, VPS_IP, "root", 22)
    return DeployContext(req, deps, node, inventory, None, "zone-1", "example.com", META, agent_key("jp-1"), AGENT, PORTS)


def test_default_deps_use_this_users_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    deps = default_deps()
    assert deps.ssh_config == Path.home() / ".ssh" / "config"
    assert deps.pub_dir == Path.home() / ".ssh" / "pub"
    assert deps.conf_path.name == "secrets.env"
    assert isinstance(deps.ui, Ui) and isinstance(deps.rng, secrets.SystemRandom)
    remote = deps.make_remote(Target("jp-1", VPS_IP, "root", 22, None, None))
    assert isinstance(remote, Remote) and remote.ssh_config == deps.ssh_config


def test_no_default_aws_credentials(no_aws_environment):
    assert _aws_default_credentials() is False


def test_default_aws_credentials_from_environment(no_aws_environment, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-example")
    assert _aws_default_credentials() is True


def test_lightsail_with_explicit_keys(no_aws_environment):
    lightsail = _make_lightsail("jp", ("AKIDEXAMPLE", "secret-example"))
    assert lightsail.region == "ap-northeast-1"


def test_save_writes_only_changes(fake_deps):
    inventory = RecordingInventory()
    ctx = context(fake_deps.deps, inventory)
    ctx.save()
    assert inventory.puts == []  # a vendor server is not recorded before the takeover gate passes
    ctx.taken_over = True
    ctx.save()
    ctx.save()
    ctx.node.ipv6 = "2001:db8::7"
    ctx.save()
    assert len(inventory.puts) == 2 and '"ipv6": "2001:db8::7"' in inventory.puts[1]


def test_target_and_exists(fake_deps):
    ssh = FakeSsh(stdout=b"no\n")
    fake_deps.deps.make_remote = lambda target: Remote(target, fake_deps.deps.ssh_config, run=ssh)
    ctx = context(fake_deps.deps)
    assert ctx.target() == Target("jp-1", VPS_IP, "root", 22, str(fake_deps.deps.pub_dir / "jp-1.pub"), AGENT)
    assert ctx.target("n.example.com").hostname == "n.example.com"
    assert ctx.exists("/opt/cnsr") is False
    assert ssh.args[-1].startswith("sh -c ") and ssh.args[-1].endswith(" sh /opt/cnsr")
    ssh.stdout = b"yes\n"
    assert ctx.exists("/opt/cnsr") is True


def test_session_socket_becomes_an_environment_reference(tmp_path):
    fixed = Path(known_sockets(tmp_path, "linux")[0])
    fixed.touch()
    session = tmp_path / "session.sock"
    session.touch()
    assert _detect_agents({"SSH_AUTH_SOCK": str(session)}, tmp_path, "linux") == [str(fixed), SESSION_AGENT]
    # a fixed socket stays a path even when SSH_AUTH_SOCK points at it
    assert _detect_agents({"SSH_AUTH_SOCK": str(fixed)}, tmp_path, "linux") == [str(fixed)]
    assert _detect_agents({}, tmp_path, "win32") == [None]


def test_default_list_keys_resolves_the_environment_reference(monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(agent, "list_keys", lambda socket: asked.append(socket) or [])
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/session.sock")
    deps = default_deps()
    deps.list_keys(SESSION_AGENT)
    deps.list_keys("/home/u/.1password/agent.sock")
    assert asked == ["/tmp/session.sock", "/home/u/.1password/agent.sock"]
