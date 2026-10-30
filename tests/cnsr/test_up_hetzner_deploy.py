"""``cnsr up --hetzner``: the server and its Primary IP, the root login, hardening and reruns."""

from __future__ import annotations

import pytest
from deploy_fakes import AGENT, agent_key, run_rm, run_up
from hetzner_fakes import HZ_IP, HZ_IPV6, FakeHetzner

from cnsr import features, localconf
from cnsr.errors import CnsrError
from cnsr.sizes import describe, pick
from cnsr.ssh.config import read_block

TOKEN = "hz-env-token-0001"
SAVED = ["已保存"]  # the console password, once hardening shows it


@pytest.fixture
def hz(fake_deps):
    """The fakes with a Hetzner token in ``HCLOUD_TOKEN``, answering the console password prompt."""
    fake_deps.hcloud_env = TOKEN
    fake_deps.terminal.inputs = list(SAVED)
    return fake_deps


def deploy(fakes, chosen=features.ALL) -> int:
    return run_up(fakes, hetzner="sg", size="1c1g", features=list(chosen))


# --- the first deployment ------------------------------------------------------------------


def test_a_first_deployment_creates_the_primary_ip_then_the_server(hz):
    assert deploy(hz) == 0
    names = hz.hetzner.names()
    assert names[:7] == [
        "pick", "instance_tagged", "ensure_key_pair", "ensure_primary_ip", "ensure_instance", "wait_running",
        "instance_ipv6",
    ]  # fmt: skip
    assert names.count("pick") == 1  # prepare chose the type already
    assert ("ensure_primary_ip", "jp-1-ip", "jp-1") in hz.hetzner.calls
    assert ("ensure_instance", "jp-1", "jp-1", "cpx02", "jp-1-ip") in hz.hetzner.calls
    node = hz.node()
    assert (node.source, node.region, node.size) == ("hetzner", "sg", "1c1g")
    assert (node.instance_name, node.static_ip_name, node.ipv4, node.ipv6) == ("jp-1", "jp-1-ip", HZ_IP, HZ_IPV6)
    assert hz.calls["forget"] == [(HZ_IP, 22)]


def test_the_primary_ip_and_server_are_on_record_before_the_server_exists(hz, monkeypatch):
    seen = []
    create = hz.hetzner.ensure_instance

    def ensure_instance(name, key, offer, primary_ip, alias=None):
        node = hz.node()
        seen.append((node.static_ip_name, node.instance_name))
        return create(name, key, offer, primary_ip, alias)

    monkeypatch.setattr(hz.hetzner, "ensure_instance", ensure_instance)
    assert deploy(hz) == 0
    assert seen == [("jp-1-ip", "jp-1")]


def test_a_full_deployment_hardens_and_deploys_xray(hz):
    assert deploy(hz) == 0
    server, node = hz.server, hz.node()
    stage = next(entry for entry in server.log if entry[3] == "harden-stage.sh")
    assert stage[:3] == ("root", 22, HZ_IP)
    assert node.ssh.user == "admin" and node.ssh.port != 22
    docker = next(entry for entry in server.log if entry[3] == "install-docker.sh")
    assert docker[:2] == ("admin", node.ssh.port)
    assert node.line and node.features == list(features.ALL)
    block = read_block(hz.deps.ssh_config, "jp-1")
    assert (block.user, block.port, block.hostname) == ("admin", node.ssh.port, node.domain)


def test_harden_only_stops_after_hardening(hz):
    assert deploy(hz, [features.HARDEN]) == 0
    node = hz.node()
    assert node.ssh.user == "admin" and node.line is None and node.features == [features.HARDEN]
    assert not hz.server.did("install-docker.sh")
    assert f"服务器 jp-1 已加固：用 ssh jp-1 登录（用户 admin，端口 {node.ssh.port}）" in hz.terminal.text


def test_the_stage_list_names_hetzner_and_the_region(hz):
    assert deploy(hz) == 0
    assert "cnsr up jp-1 · Hetzner 新加坡" in hz.terminal.screen


# --- the login -----------------------------------------------------------------------------


def test_a_new_server_that_rejects_the_key_gets_the_boot_wait_not_a_password_login(hz):
    hz.server.key_installed = False  # sshd is up before the key is in place

    def key_arrives(seconds):
        hz.sleeps.append(seconds)
        hz.server.key_installed = True

    hz.deps.sleep = key_arrives
    assert deploy(hz, [features.HARDEN]) == 0
    assert hz.calls["install_key"] == [] and "root 密码" not in hz.terminal.text
    assert "等待 SSH 就绪" in hz.terminal.text


# --- reruns --------------------------------------------------------------------------------


def test_a_rerun_reuses_the_server_without_choosing_a_type(hz):
    assert deploy(hz) == 0
    hz.hetzner.calls.clear()
    assert run_up(hz, hetzner="sg") == 0
    names = hz.hetzner.names()
    assert "offers" not in names and "pick" not in names
    assert ("ensure_instance", "jp-1", "jp-1", None, "jp-1-ip") in hz.hetzner.calls
    assert hz.calls["forget"] == [(HZ_IP, 22)]  # the server was already known
    assert hz.hetzner.servers == {"jp-1": True}


def test_a_rerun_whose_server_is_gone_creates_it_from_the_recorded_size(hz):
    assert deploy(hz, [features.HARDEN]) == 0
    hz.hetzner.servers.pop("jp-1")  # deleted by hand; its Primary IP is free again
    hz.hetzner.assigned["jp-1-ip"] = None
    hz.hetzner.calls.clear()
    hz.terminal.inputs = list(SAVED)
    hardened = hz.node().ssh
    assert (hardened.user, hardened.port) != ("root", 22)
    hz.server.log.clear()
    assert run_up(hz, hetzner="sg", features=[features.HARDEN]) == 0
    assert ("pick", "1c1g") in hz.hetzner.calls
    offer = pick(hz.hetzner.stock, "1c1g")
    assert f"服务器 jp-1 不在了，按记录的规格重新开一台：{describe(offer)}" in hz.terminal.screen
    assert ("ensure_instance", "jp-1", "jp-1", "cpx02", "jp-1-ip") in hz.hetzner.calls
    assert hz.calls["forget"].count((HZ_IP, 22)) == 2
    # The new server only knows root on 22: it is logged into that way and hardened again.
    logins = [entry[:2] for entry in hz.server.log if entry[0]]
    assert logins[0] == ("root", 22) and hz.server.did("harden-stage.sh")
    node = hz.node()
    assert node.ssh.user == "admin" and node.ssh.port != 22 and node.features == [features.HARDEN]


def test_a_rerun_that_recreates_the_server_cleans_it_up_when_it_fails(hz):
    assert deploy(hz, [features.HARDEN]) == 0
    hz.hetzner.delete_instance("jp-1")  # deleted by hand; its Primary IP went with it (auto_delete)
    hz.terminal.inputs = list(SAVED)
    hz.server.fail_on["preflight.sh"] = CnsrError("预检失败")
    assert run_up(hz, hetzner="sg", features=[features.HARDEN]) == 1
    assert hz.hetzner.servers == {} and hz.hetzner.ips == {} and hz.node() is None
    assert "已清理干净，不会继续计费" in hz.terminal.text


def test_a_rerun_buys_nothing_when_the_server_is_in_the_saved_tokens_project(hz):
    assert deploy(hz) == 0
    values = localconf.load(hz.deps.conf_path)
    localconf.save(hz.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token-0002"})
    hz.other_projects["hz-saved-token-0002"], hz.hetzner = hz.hetzner, FakeHetzner()
    assert run_up(hz, hetzner="sg") == 1
    assert "本机保存的 token 所在的项目里有" in hz.terminal.text
    assert "pick" not in hz.hetzner.names() and hz.hetzner.servers == {} and hz.hetzner.ips == {}
    assert hz.node() is not None


def test_a_stranger_server_of_the_alias_stops_the_run(hz):
    hz.hetzner.servers["jp-1"] = False
    assert deploy(hz) == 1
    assert "不是 cnsr 创建的" in hz.terminal.text
    assert hz.hetzner.servers == {"jp-1": False}  # left alone, also by the cleanup


def test_an_alias_with_an_underscore_gets_a_hostname_as_its_server_name(hz):
    hz.agents[AGENT].append(agent_key("de_hz"))
    assert run_up(hz, "de_hz", hetzner="sg", size="1c1g") == 0
    node = hz.node("de_hz")
    assert node.instance_name == "de-hz" and "de-hz" in hz.hetzner.servers
    assert node.static_ip_name == "de_hz-ip"
    hz.terminal.inputs = ["1"]
    assert run_rm(hz, "de_hz") == 0
    assert hz.hetzner.servers == {} and hz.node("de_hz") is None
