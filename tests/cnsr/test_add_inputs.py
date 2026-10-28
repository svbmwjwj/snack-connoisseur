"""Tests for what ``cnsr up`` asks before deploying: alias, source, credentials, agent and key."""

from __future__ import annotations

import sys

import pytest
from deploy_fakes import AGENT, VPS_IP, agent_key, preflight_output, run_up
from key_samples import RSA

from cnsr import localconf
from cnsr.ssh.agent import SESSION_AGENT
from cnsr.ssh.config import HostBlock, read_block, write_block

OTHER_AGENT = "/home/u/.1password/agent.sock"


def test_invalid_alias_argument_fails(fake_deps):
    assert run_up(fake_deps, alias="Bad Alias", aws="jp") == 1
    assert "别名只能包含" in fake_deps.terminal.text


def test_alias_source_and_region_are_asked(fake_deps):
    # alias (first one invalid), source: 2 = AWS, region: 1 = jp
    fake_deps.terminal.inputs = ["Bad", "jp-1", "2", "1"]
    assert run_up(fake_deps, alias=None) == 0
    assert "别名只能包含" in fake_deps.terminal.text
    assert fake_deps.calls["make_lightsail"] == [("jp", None)]
    assert fake_deps.node().region == "jp"


def test_ip_argument_must_be_ipv4(fake_deps):
    assert run_up(fake_deps, ip="2001:db8::1") == 1
    assert "IPv4" in fake_deps.terminal.text


def test_existing_node_with_other_source_fails(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert "已存在" in fake_deps.terminal.text
    assert fake_deps.node().source == "aws"


def test_aws_credentials_asked_and_saved_when_chain_empty(fake_deps):
    fake_deps.aws_chain = False
    fake_deps.terminal.inputs = ["AKIDEXAMPLE", "secret-example"]
    assert run_up(fake_deps, aws="jp") == 0
    saved = localconf.load(fake_deps.deps.conf_path)
    assert (saved["AWS_ACCESS_KEY_ID"], saved["AWS_SECRET_ACCESS_KEY"]) == ("AKIDEXAMPLE", "secret-example")
    assert saved["CLOUDFLARE_API_TOKEN"] == "t-secret"  # merged, not replaced
    assert fake_deps.calls["make_lightsail"] == [("jp", ("AKIDEXAMPLE", "secret-example"))]
    assert fake_deps.lightsail.names()[0] == "pick_blueprint"  # the keys work before they are saved


def test_aws_keys_from_secrets_are_passed_explicitly(fake_deps):
    fake_deps.aws_chain = False
    values = localconf.load(fake_deps.deps.conf_path)
    localconf.save(fake_deps.deps.conf_path, {**values, "AWS_ACCESS_KEY_ID": "AKIDSAVED", "AWS_SECRET_ACCESS_KEY": "s"})
    assert run_up(fake_deps, aws="sg") == 0
    assert fake_deps.calls["make_lightsail"] == [("sg", ("AKIDSAVED", "s"))]
    assert fake_deps.terminal.prompts == []


def test_aws_default_chain_comes_first(fake_deps):
    values = localconf.load(fake_deps.deps.conf_path)
    localconf.save(fake_deps.deps.conf_path, {**values, "AWS_ACCESS_KEY_ID": "AKIDSAVED", "AWS_SECRET_ACCESS_KEY": "s"})
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.calls["make_lightsail"] == [("jp", None)]


def test_several_agents_ask_once_and_remember(fake_deps):
    fake_deps.agents = {AGENT: [agent_key("jp-1")], OTHER_AGENT: [agent_key("jp-1")]}
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp") == 0
    assert localconf.load(fake_deps.deps.conf_path)["CNSR_SSH_AGENT"] == OTHER_AGENT
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_agent == OTHER_AGENT

    prompts = len(fake_deps.terminal.prompts)
    assert run_up(fake_deps, aws="jp") == 0
    assert len(fake_deps.terminal.prompts) == prompts


def test_agents_without_keys_are_skipped(fake_deps):
    fake_deps.agents = {AGENT: [], OTHER_AGENT: [agent_key("jp-1")]}
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.terminal.prompts == []
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_agent == OTHER_AGENT
    assert "CNSR_SSH_AGENT" not in localconf.load(fake_deps.deps.conf_path)


def test_no_key_anywhere_stops(fake_deps):
    fake_deps.agents = {AGENT: []}
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="jp") == 1
    assert "SSH agent" in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == []


def test_a_typed_agent_socket_may_start_with_a_tilde(fake_deps, monkeypatch):
    monkeypatch.setenv("HOME", str(fake_deps.home))
    typed = str(fake_deps.home / "agents" / "my.sock")
    fake_deps.agents = {AGENT: [], typed: [agent_key("jp-1")]}
    fake_deps.deps.detect_agents = lambda: [AGENT]
    fake_deps.terminal.inputs = ["2", "~/agents/my.sock"]  # another agent, not a new key
    assert run_up(fake_deps, aws="jp") == 0
    assert localconf.load(fake_deps.deps.conf_path)["CNSR_SSH_AGENT"] == typed
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_agent == typed


def test_missing_key_lists_agent_keys(fake_deps):
    other, chosen = agent_key("other", RSA), agent_key("tokyo_key")
    fake_deps.agents = {AGENT: [other, chosen]}
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp") == 0
    text = fake_deps.terminal.text
    assert "建议" in text and "jp-1" in text and "1. other" in text
    pub = fake_deps.deps.pub_dir / "tokyo_key.pub"
    assert pub.read_text() == chosen.line + "\n"
    assert fake_deps.node().ssh.key_name == "tokyo_key"
    assert ("ensure_key_pair", "tokyo_key", chosen.line) in fake_deps.lightsail.calls
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_file == str(pub)


def test_pub_file_with_another_key_stops(fake_deps):
    fake_deps.deps.pub_dir.mkdir(parents=True)
    (fake_deps.deps.pub_dir / "jp-1.pub").write_text(RSA + "\n")
    assert run_up(fake_deps, aws="jp") == 1
    assert "jp-1.pub" in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_ssh_directory_is_private(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    assert (fake_deps.home / ".ssh").stat().st_mode & 0o777 == 0o700


def test_vps_reuses_ssh_config_block(fake_deps):
    pub = fake_deps.deps.pub_dir / "jp-1.pub"
    write_block(fake_deps.deps.ssh_config, HostBlock("jp-1", VPS_IP, "admin", 2222, str(pub), AGENT))
    fake_deps.server.preflight = preflight_output(listen=("0.0.0.0:2222",), user="admin")
    fake_deps.terminal.inputs = ["1"]  # source: an existing server
    assert run_up(fake_deps, alias="jp-1") == 0
    logins = {(user, port, host) for user, port, host, _ in fake_deps.server.log if user}
    assert ("admin", 2222, VPS_IP) in logins and not any(user == "root" for user, _, _ in logins)
    assert not fake_deps.server.did("harden-stage.sh")  # already a normal user: no hardening
    node = fake_deps.node()
    assert (node.ipv4, node.ssh.user, node.ssh.port) == (VPS_IP, "admin", 2222)


def test_rerun_with_another_key_records_it(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    fake_deps.agents = {AGENT: [agent_key("jp_new", RSA)]}
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.node().ssh.key_name == "jp_new"


def test_session_agent_is_kept_as_an_environment_reference(fake_deps):
    targets = []
    make_remote = fake_deps.deps.make_remote
    fake_deps.deps.make_remote = lambda target: targets.append(target) or make_remote(target)
    fake_deps.agents = {SESSION_AGENT: [agent_key("jp-1")], AGENT: [agent_key("jp-1")]}
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert "SSH_AUTH_SOCK" in fake_deps.terminal.text
    assert localconf.load(fake_deps.deps.conf_path)["CNSR_SSH_AGENT"] == SESSION_AGENT
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_agent == SESSION_AGENT
    assert 'IdentityAgent "SSH_AUTH_SOCK"' in fake_deps.deps.ssh_config.read_text()
    assert targets and {target.identity_agent for target in targets} == {SESSION_AGENT}


def test_stale_remembered_agent_is_replaced(fake_deps):
    values = localconf.load(fake_deps.deps.conf_path)
    localconf.save(fake_deps.deps.conf_path, {**values, "CNSR_SSH_AGENT": "/home/u/gone.sock"})
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.terminal.prompts == []
    assert localconf.load(fake_deps.deps.conf_path)["CNSR_SSH_AGENT"] == AGENT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_missing_ssh_config_is_created_empty_and_private(fake_deps):
    fake_deps.server.preflight = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"))
    fake_deps.terminal.inputs = ["", ""]
    assert run_up(fake_deps, ip=VPS_IP) == 1
    ssh_config = fake_deps.deps.ssh_config
    assert ssh_config.read_bytes() == b"" and ssh_config.stat().st_mode & 0o777 == 0o600
    assert ssh_config.parent.stat().st_mode & 0o777 == 0o700
