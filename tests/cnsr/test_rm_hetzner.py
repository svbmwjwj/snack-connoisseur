"""``cnsr rm`` on Hetzner: which server, Primary IP and firewall may be deleted, and what a rerun finishes.

Every resource carries the label ``cnsr=<alias>``, so a name the record lacks is looked up by
the name cnsr gives it and deleted only when its label proves it the node's.
"""

from __future__ import annotations

import pytest
from deploy_fakes import run_up
from hetzner_fakes import FakeHetzner
from rm_harness import remove

from cnsr import localconf
from cnsr.ssh.config import read_block

HETZNER_TOKEN = "hz-env-token-0001"
ALL_CALLS = [
    ("instance_tagged", "jp-1", "jp-1"),
    ("delete_instance", "jp-1"),
    ("delete_primary_ip", "jp-1-ip", "jp-1"),
    ("delete_firewall", "jp-1"),
]


@pytest.fixture
def hz(fake_deps):
    """jp-1 deployed on Hetzner, the calls that took forgotten."""
    fake_deps.hcloud_env = HETZNER_TOKEN
    fake_deps.terminal.inputs = ["已保存"]
    assert run_up(fake_deps, hetzner="sg", size="1c1g") == 0
    fake_deps.hetzner.calls.clear()
    fake_deps.calls.clear()
    return fake_deps


def names_in_record(fakes) -> tuple[str | None, str | None]:
    node = fakes.node()
    return node.instance_name, node.static_ip_name


def forget_names(fakes) -> None:
    """The record as a run killed right after creating the server leaves it."""
    node = fakes.node()
    node.instance_name = node.static_ip_name = None
    fakes.store(node)


def lose_server(fakes) -> None:
    """The user deleted the server by hand; cnsr's Primary IP went with it (auto_delete)."""
    fakes.hetzner.delete_instance("jp-1")
    fakes.hetzner.calls.clear()


def empty(fakes) -> bool:
    hetzner = fakes.hetzner
    return hetzner.servers == {} and hetzner.ips == {} and hetzner.firewalls == {}


# --- the whole removal


def test_rm_deletes_the_server_then_the_primary_ip_then_the_firewall(hz):
    code, out = remove(hz)
    assert code == 0
    assert hz.hetzner.calls == ALL_CALLS
    assert empty(hz) and hz.node() is None and hz.dns.records == {}
    assert read_block(hz.deps.ssh_config, "jp-1") is None
    assert hz.calls["make_hetzner"] == [("sg", HETZNER_TOKEN)]


def test_the_plan_lists_the_server_region_primary_ip_and_firewall_and_keeps_the_ssh_key(hz):
    _, out = remove(hz)
    plan = out[: out.index("保留不动")]
    assert "Hetzner 服务器 jp-1（区域 sg" in plan and "Primary IP jp-1-ip" in plan and "防火墙 jp-1-fw" in plan
    assert "SSH key" in out[out.index("保留不动") :]


def test_the_token_never_shows(hz):
    _, out = remove(hz)
    assert HETZNER_TOKEN not in out


# --- which names are used


def test_the_record_decides_which_names_are_deleted(hz):
    node = hz.node()
    node.instance_name, node.static_ip_name = "jp-1-old", "jp-1-old-ip"
    hz.store(node)
    hz.hetzner.servers = {"jp-1-old": True}
    hz.hetzner.ips, hz.hetzner.assigned = {"jp-1-old-ip": True}, {"jp-1-old-ip": None}
    hz.hetzner.attached = {}
    assert remove(hz)[0] == 0
    assert hz.hetzner.calls == [
        ("instance_tagged", "jp-1-old", "jp-1"),
        ("delete_instance", "jp-1-old"),
        ("delete_primary_ip", "jp-1-old-ip", "jp-1"),
        ("delete_firewall", "jp-1"),
    ]
    assert empty(hz) and hz.node() is None


def test_without_recorded_names_the_server_and_ip_named_after_the_alias_are_deleted(hz):
    forget_names(hz)
    assert remove(hz)[0] == 0
    assert hz.hetzner.calls == ALL_CALLS
    assert empty(hz) and hz.node() is None


def test_a_server_without_cnsrs_label_is_never_deleted(hz):
    hz.hetzner.servers["jp-1"] = False
    code, out = remove(hz)
    assert code == 1
    assert "服务器 jp-1 不是 cnsr 创建的" in out and "没有删干净" in out
    assert ("delete_instance", "jp-1") not in hz.hetzner.calls
    assert hz.hetzner.servers == {"jp-1": False}
    assert names_in_record(hz) == ("jp-1", "jp-1-ip")  # the IP is still assigned to it: not deleted


def test_without_a_recorded_server_a_stranger_of_the_alias_is_not_the_nodes(hz):
    forget_names(hz)
    lose_server(hz)
    hz.hetzner.servers["jp-1"] = False
    assert remove(hz)[0] == 0
    assert hz.hetzner.servers == {"jp-1": False} and hz.node() is None
    assert ("delete_instance", "jp-1") not in hz.hetzner.calls


def test_a_primary_ip_without_cnsrs_label_is_left_alone(hz):
    lose_server(hz)
    hz.hetzner.ips["jp-1-ip"], hz.hetzner.assigned["jp-1-ip"] = False, None
    code, out = remove(hz)
    assert code == 1
    assert "Primary IP jp-1-ip 不是 cnsr 创建的" in out
    assert hz.hetzner.ips == {"jp-1-ip": False}
    assert names_in_record(hz) == (None, "jp-1-ip")


# --- what is already gone


def test_a_missing_server_is_skipped_but_its_recorded_primary_ip_is_deleted(hz):
    lose_server(hz)
    hz.hetzner.ips["jp-1-ip"], hz.hetzner.assigned["jp-1-ip"] = True, None  # one without auto_delete
    assert remove(hz)[0] == 0
    assert hz.hetzner.calls == [("instance_tagged", "jp-1", "jp-1"), *ALL_CALLS[2:]]
    assert empty(hz) and hz.node() is None


def test_everything_already_gone_needs_nothing(hz):
    lose_server(hz)
    hz.hetzner.firewalls.clear()
    assert remove(hz)[0] == 0
    assert hz.node() is None


# --- what fails, and the rerun that finishes it


def test_a_server_that_cannot_be_deleted_keeps_everything_and_a_rerun_finishes(hz):
    hz.hetzner.delete_instance_errors = ["Hetzner API 请求失败（HTTP 423）：资源被锁定，稍后重试"]
    code, out = remove(hz)
    assert code == 1
    assert "HTTP 423" in out and "没有删干净" in out
    assert names_in_record(hz) == ("jp-1", "jp-1-ip")
    assert "delete_primary_ip" not in hz.hetzner.names() and "delete_firewall" not in hz.hetzner.names()
    assert hz.dns.records == {} and read_block(hz.deps.ssh_config, "jp-1") is None

    hz.hetzner.delete_instance_errors = []
    hz.hetzner.calls.clear()
    assert remove(hz)[0] == 0
    assert hz.hetzner.calls == ALL_CALLS
    assert empty(hz) and hz.node() is None


def test_a_primary_ip_that_cannot_be_deleted_keeps_its_name_and_a_rerun_retries_it(hz):
    lose_server(hz)
    hz.hetzner.ips["jp-1-ip"], hz.hetzner.assigned["jp-1-ip"] = True, None
    hz.hetzner.delete_ip_errors = ["Hetzner API 请求失败（HTTP 423）：资源被锁定，稍后重试"]
    code, out = remove(hz)
    assert code == 1 and "HTTP 423" in out
    assert names_in_record(hz) == (None, "jp-1-ip")  # the server is done
    assert hz.hetzner.firewalls == {}  # the firewall goes all the same

    hz.hetzner.delete_ip_errors = []
    hz.hetzner.calls.clear()
    assert remove(hz)[0] == 0
    assert hz.hetzner.calls == [("instance_tagged", "jp-1", "jp-1"), *ALL_CALLS[2:]]
    assert empty(hz) and hz.node() is None


def test_a_firewall_that_cannot_be_deleted_keeps_the_record_and_a_rerun_retries_it(hz):
    hz.hetzner.delete_firewall_errors = ["防火墙 jp-1-fw 还挂在其他资源上，Hetzner 拒绝删除，请到 Hetzner 控制台处理"]
    code, out = remove(hz)
    assert code == 1 and "拒绝删除" in out
    assert names_in_record(hz) == (None, None)
    assert hz.hetzner.servers == {} and hz.hetzner.ips == {}

    hz.hetzner.delete_firewall_errors = []
    hz.hetzner.calls.clear()
    assert remove(hz)[0] == 0
    assert hz.hetzner.firewalls == {} and hz.node() is None


# --- credentials and the record


def test_missing_credentials_keep_the_record_until_a_token_is_saved(hz):
    hz.hcloud_env = None
    code, out = remove(hz)
    assert code == 1
    assert "没有找到 Hetzner token" in out and "HCLOUD_TOKEN" in out
    assert hz.calls["make_hetzner"] == [] and hz.hetzner.calls == []
    assert hz.node() is not None and hz.dns.records == {}

    values = localconf.load(hz.deps.conf_path)
    localconf.save(hz.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token-0002"})
    assert remove(hz)[0] == 0
    assert hz.calls["make_hetzner"] == [("sg", "hz-saved-token-0002")]
    assert empty(hz) and hz.node() is None


def test_a_record_without_a_region_is_reported(hz):
    node = hz.node()
    node.region = None
    hz.store(node)
    code, out = remove(hz)
    assert code == 1
    assert "节点记录里没有 Hetzner 区域" in out
    assert hz.hetzner.servers == {"jp-1": True} and hz.node() is not None


# --- a token of another project


def saved_token_of_the_real_project(fakes) -> FakeHetzner:
    """The node's project behind the saved token; ``HCLOUD_TOKEN`` names another, empty one."""
    real = fakes.hetzner
    values = localconf.load(fakes.deps.conf_path)
    localconf.save(fakes.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token-0002"})
    fakes.other_projects["hz-saved-token-0002"] = real
    fakes.hetzner = FakeHetzner()
    return real


def test_a_server_only_the_saved_token_sees_keeps_the_record(hz):
    real = saved_token_of_the_real_project(hz)
    code, out = remove(hz)
    assert code == 1
    assert "环境变量 HCLOUD_TOKEN 所在的 Hetzner 项目里没有服务器 jp-1" in out and "unset HCLOUD_TOKEN" in out
    assert real.servers == {"jp-1": True} and hz.node() is not None
    assert HETZNER_TOKEN not in out and "hz-saved-token-0002" not in out


def test_a_server_gone_from_both_projects_is_gone(hz):
    real = saved_token_of_the_real_project(hz)
    lose_server_in(real)
    assert remove(hz)[0] == 0
    assert hz.node() is None


def lose_server_in(hetzner: FakeHetzner) -> None:
    hetzner.delete_instance("jp-1")
    hetzner.firewalls.clear()
