"""Someone without a fitting agent key lets cnsr up make one with ssh-keygen."""

from __future__ import annotations

from deploy_fakes import AGENT, agent_key, run_up, run_rm

from cnsr.ssh.agent import SESSION_AGENT
from cnsr.ssh.config import read_block


def key_path(fake_deps):
    return fake_deps.deps.key_dir / "jp-1"


def test_without_any_agent_key_a_new_key_is_offered_and_used(fake_deps):
    fake_deps.agents = {}
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    path = key_path(fake_deps)
    assert fake_deps.calls["generate_key"] == [(path, "jp-1")]
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    block = read_block(fake_deps.deps.ssh_config, "jp-1")
    assert block.identity_file == str(path) and block.identity_agent is None
    assert fake_deps.node().ssh.key_name == "jp-1"
    assert ("ensure_key_pair", "jp-1", agent_key("jp-1").line) in fake_deps.lightsail.calls
    text = fake_deps.terminal.text
    assert "生成一把新钥匙" in text and f"请把私钥 {path} 备份到安全的地方" in text
    assert fake_deps.calls["add_key_to_agent"] == []  # no passphrase: ssh reads the file itself


def test_an_agent_without_the_nodes_key_offers_a_new_one_after_its_keys(fake_deps):
    fake_deps.agents = {AGENT: [agent_key("other")]}
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp") == 0
    assert "1. other" in fake_deps.terminal.text and f"2. 生成一把新钥匙（{key_path(fake_deps)}）" in fake_deps.terminal.text
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_file == str(key_path(fake_deps))


def test_a_key_with_a_passphrase_goes_to_the_session_agent(fake_deps):
    fake_deps.agents = {SESSION_AGENT: []}
    fake_deps.passphrase = True
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.calls["add_key_to_agent"] == [key_path(fake_deps)]
    block = read_block(fake_deps.deps.ssh_config, "jp-1")
    assert block.identity_file == str(key_path(fake_deps)) and block.identity_agent == SESSION_AGENT


def test_a_key_with_a_passphrase_and_no_agent_to_hold_it_stops(fake_deps):
    fake_deps.agents = {}
    fake_deps.passphrase = True
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 1
    assert "ssh-agent" in fake_deps.terminal.text and fake_deps.lightsail.calls == []


def test_a_rerun_uses_the_key_cnsr_made_without_asking(fake_deps):
    fake_deps.agents = {}
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    prompts = len(fake_deps.terminal.prompts)
    assert run_up(fake_deps, aws="jp") == 0
    assert len(fake_deps.terminal.prompts) == prompts and len(fake_deps.calls["generate_key"]) == 1


def test_rm_keeps_the_key_cnsr_made(fake_deps):
    fake_deps.agents = {}
    fake_deps.terminal.inputs = ["1", "jp-1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert run_rm(fake_deps) == 0
    assert key_path(fake_deps).exists()
    assert f"{key_path(fake_deps)}（cnsr 生成的私钥" in fake_deps.terminal.text


def test_without_a_terminal_no_key_is_made(fake_deps):
    fake_deps.agents = {}
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="jp") == 1
    assert fake_deps.calls["generate_key"] == [] and fake_deps.lightsail.calls == []


def test_a_key_cnsr_made_before_is_used_instead_of_made_again(fake_deps):
    # The record names an agent key that is gone, but cnsr made ~/.ssh/cnsr/jp-1 once before.
    fake_deps.agents = {AGENT: [agent_key("other")]}
    fake_deps.deps.key_dir.mkdir(parents=True)
    fake_deps._generate_key(key_path(fake_deps), "jp-1")
    fake_deps.calls["generate_key"].clear()
    from cnsr.commands import add_keygen

    agent, key = add_keygen.make(fake_deps.deps, "jp-1")
    assert fake_deps.calls["generate_key"] == [] and key.comment == "jp-1"


def test_on_windows_the_named_pipe_agent_holds_a_key_with_a_passphrase(fake_deps, monkeypatch):
    monkeypatch.setattr("cnsr.commands.add_keygen.sys.platform", "win32")
    fake_deps.agents = {None: []}  # what detect_agents gives on Windows: the system agent's pipe
    fake_deps.passphrase = True
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.calls["add_key_to_agent"] == [key_path(fake_deps)]
    assert read_block(fake_deps.deps.ssh_config, "jp-1").identity_agent is None
