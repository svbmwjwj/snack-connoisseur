"""Tests for ``cnsr rm``: what it lists, the order of removal and what a partial failure leaves behind.

The AWS side (which instance and static IP may be deleted) is in ``test_rm_aws.py``.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from deploy_fakes import VPS_IP
from rm_harness import CONFIRM_PROMPT, deploy_aws, deploy_vps, entry_deleted, remove

from cnsr.commands import rm
from cnsr.errors import CnsrError
from cnsr.ssh.config import read_block, write_block


# --- nothing to remove


def test_unknown_alias(fake_deps):
    deploy_aws(fake_deps)
    code, out = remove(fake_deps, "x")
    assert code == 1
    assert "没有名为 x 的节点" in out
    assert fake_deps.node() is not None
    assert fake_deps.lightsail.calls == [] and fake_deps.dns.requests == []
    assert fake_deps.terminal.prompts == []


def test_no_control_plane_is_not_created(fake_deps):
    code, out = remove(fake_deps)
    assert code == 1
    assert "没有名为 jp-1 的节点" in out
    assert {method for method, _ in fake_deps.cloudflare.calls()} == {"GET"}
    assert fake_deps.cloudflare.namespaces == {}
    assert fake_deps.cloudflare.scripts == {} and fake_deps.cloudflare.domains == []


def test_no_cloudflare_settings_means_no_nodes(fake_deps):
    fake_deps.deps.conf_path.unlink()
    code, out = remove(fake_deps)
    assert code == 1
    assert "没有名为 jp-1 的节点" in out
    assert fake_deps.calls["make_cf"] == []


# --- the listing and the confirmation


def test_confirmation_requires_exact_alias(fake_deps):
    deploy_aws(fake_deps)
    code, out = remove(fake_deps, answers=["jp", "jp-1"])
    assert code == 0
    assert fake_deps.terminal.prompts == [CONFIRM_PROMPT, CONFIRM_PROMPT]
    assert "请原样输入“jp-1”" in out
    assert fake_deps.node() is None


def test_noninteractive_deletes_nothing(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    code, out = remove(fake_deps, answers=[])
    assert code == 1
    assert "缺少参数" in out
    assert fake_deps.node() is not None
    assert fake_deps.lightsail.calls == [] and fake_deps.dns.requests == []


def test_aws_listing_comes_before_the_confirmation(fake_deps):
    deploy_aws(fake_deps)
    node = fake_deps.node()
    assert remove(fake_deps)[0] == 0
    (listed,) = fake_deps.terminal.seen
    for fragment in ("Lightsail 实例 jp-1", "区域 jp", "静态 IP jp-1-ip", node.domain, "A、AAAA", "SSH 别名 jp-1", "节点登记"):
        assert fragment in listed
    assert "保留" in listed and "公钥" in listed and "key pair" in listed


def test_vps_listing_says_the_server_stays(fake_deps):
    deploy_vps(fake_deps)
    assert remove(fake_deps)[0] == 0
    (listed,) = fake_deps.terminal.seen
    assert f"服务器 {VPS_IP} 本身不动" in listed
    assert "Lightsail" not in listed and "静态 IP" not in listed


# --- full removal


def test_aws_full_removal(fake_deps):
    deploy_aws(fake_deps)
    node = fake_deps.node()
    pub = fake_deps.deps.pub_dir / f"{node.ssh.key_name}.pub"
    pub_text = pub.read_bytes()

    code, out = remove(fake_deps)

    assert code == 0
    assert "已删除 SSH 别名 jp-1" in out and "节点 jp-1 已删除" in out
    assert fake_deps.lightsail.calls == [
        ("instance_tagged", "jp-1", "jp-1"),
        ("static_ip_attachment", "jp-1-ip"),
        ("release_static_ip", "jp-1-ip"),
        ("delete_instance", "jp-1"),
    ]
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {}
    assert fake_deps.calls["make_lightsail"] == [("jp", None)]
    assert fake_deps.dns.records == {}
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None
    assert fake_deps.node() is None and entry_deleted(fake_deps)
    assert pub.read_bytes() == pub_text
    assert fake_deps.server.log == []  # the server is never contacted


def test_rm_deletes_the_rotate_state(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.kv()["state:jp-1"] = '{"version": 1, "exhausted": true}'
    fake_deps.kv()["state:other"] = '{"version": 1}'
    code, _ = remove(fake_deps)
    assert code == 0
    assert "state:jp-1" not in fake_deps.kv() and "state:other" in fake_deps.kv()


def test_rm_takes_the_node_out_of_the_index(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.kv()["index:nodes"] = json.dumps(["jp-1", "other"])
    code, _ = remove(fake_deps)
    assert code == 0
    assert json.loads(fake_deps.kv()["index:nodes"]) == ["other"]


def test_the_rotate_state_stays_while_the_record_does(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.kv()["state:jp-1"] = '{"version": 1}'
    fake_deps.dns.deny("DELETE", "AAAA")
    code, _ = remove(fake_deps)
    assert code == 1
    assert "state:jp-1" in fake_deps.kv() and fake_deps.node() is not None


def test_a_partial_rm_stops_serving_the_nodes_line(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.delete_instance_errors = ["删除实例 jp-1 失败：Throttling"]
    code, _ = remove(fake_deps)
    assert code == 1
    assert fake_deps.node() is not None and fake_deps.node().line is None  # out of cnsr.snippet


def test_rm_goes_on_when_the_line_cannot_be_withdrawn_first(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.cloudflare.deny("PUT", "/accounts/acct-1/storage/kv/namespaces/ns-1/values/node:jp-1", 500)
    remove(fake_deps)
    assert fake_deps.lightsail.instances == {}  # what costs money still goes


def test_a_state_written_back_after_rm_does_not_reach_the_next_add(fake_deps):
    deploy_aws(fake_deps)
    assert remove(fake_deps)[0] == 0
    # A device's report the Worker took on a cached copy of the deleted record, a minute later.
    fake_deps.kv()["state:jp-1"] = '{"version": 1, "request": {"at": 1, "version": 1}}'
    deploy_aws(fake_deps)
    assert "state:jp-1" not in fake_deps.kv()


def test_a_rotate_state_that_cannot_be_deleted_keeps_the_record(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.cloudflare.deny("DELETE", "/accounts/acct-1/storage/kv/namespaces/ns-1/values/state:jp-1", 500)
    code, out = remove(fake_deps)
    assert code == 1 and "没有删干净" in out
    assert fake_deps.node() is not None and not entry_deleted(fake_deps)


def test_vps_does_not_touch_lightsail(fake_deps):
    deploy_vps(fake_deps)
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [] and fake_deps.calls["make_lightsail"] == []
    assert fake_deps.dns.records == {}
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None
    assert fake_deps.node() is None
    assert fake_deps.server.log == []


@pytest.mark.parametrize("domain_resolves", [True, False])
def test_the_alias_goes_whether_it_names_the_ip_or_the_domain(fake_deps, domain_resolves):
    fake_deps.local_dns = domain_resolves  # without it the alias stays on the IP
    deploy_vps(fake_deps)
    block = read_block(fake_deps.deps.ssh_config, "jp-1")
    assert block.hostname == (fake_deps.node().domain if domain_resolves else VPS_IP)
    assert remove(fake_deps)[0] == 0
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None


# --- the SSH alias


def test_an_alias_that_points_elsewhere_is_kept(fake_deps):
    deploy_aws(fake_deps)
    config = fake_deps.deps.ssh_config
    write_block(config, dataclasses.replace(read_block(config, "jp-1"), hostname="203.0.113.99"))
    before = config.read_bytes()
    code, out = remove(fake_deps)
    assert code == 0
    assert config.read_bytes() == before
    assert "SSH 别名 jp-1 指向 203.0.113.99" in out and "保留" in out
    assert fake_deps.node() is None


def test_another_block_for_the_same_alias_is_never_touched(fake_deps):
    deploy_aws(fake_deps)
    config = fake_deps.deps.ssh_config
    users = "Host jp-1\n    HostName 198.51.100.7\n    User deploy\n    IdentityFile ~/.ssh/work\n"
    config.write_text(config.read_text() + "\n" + users)  # cnsr's block comes first, as ssh reads it
    code, out = remove(fake_deps)
    assert code == 0
    assert "已删除 SSH 别名 jp-1" in out
    assert config.read_text() == users
    assert fake_deps.node() is None


def test_a_missing_alias_is_not_a_failure(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.deps.ssh_config.write_text("Host other\n    Port 2200\n")
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.deps.ssh_config.read_text() == "Host other\n    Port 2200\n"
    assert fake_deps.node() is None


def test_nothing_is_claimed_when_remove_block_deletes_nothing(fake_deps, monkeypatch):
    deploy_aws(fake_deps)
    monkeypatch.setattr(rm, "remove_block", lambda path, alias, hostnames=None: False)
    code, out = remove(fake_deps)
    assert code == 0
    assert "已删除 SSH 别名" not in out and "节点 jp-1 已删除" in out


def test_an_alias_without_hostname_is_left_alone(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.deps.ssh_config.write_text("Host jp-1\n    ForwardAgent yes\n")
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.deps.ssh_config.read_text() == "Host jp-1\n    ForwardAgent yes\n"


def test_an_ssh_config_that_cannot_be_written_keeps_the_record(fake_deps, monkeypatch):
    def refuse(path, alias, hostnames=None):
        raise CnsrError(f"无法写入 SSH 配置文件 {path}。")

    deploy_aws(fake_deps)
    monkeypatch.setattr(rm, "remove_block", refuse)
    code, out = remove(fake_deps)
    assert code == 1
    assert "无法写入 SSH 配置文件" in out
    assert fake_deps.node() is not None
    assert fake_deps.dns.records == {}  # the steps before it are not undone


# --- partial failures


def test_dns_failure_keeps_inventory_and_a_rerun_retries_only_that(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.dns.deny("DELETE", "AAAA")

    code, out = remove(fake_deps)

    assert code == 1
    assert "没有删干净" in out and "AAAA" in out and "重新运行 cnsr rm jp-1" in out
    node = fake_deps.node()
    assert node is not None
    assert not entry_deleted(fake_deps)
    assert list(node.dns_record_ids) == ["AAAA"]
    assert (node.instance_name, node.static_ip_name) == (None, None)  # the AWS part is done and recorded as done
    assert fake_deps.dns.of_type("A") == [] and len(fake_deps.dns.of_type("AAAA")) == 1
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None  # the later steps still run
    assert fake_deps.lightsail.ips == {} and fake_deps.lightsail.instances == {}

    fake_deps.dns.allow("DELETE", "AAAA")
    fake_deps.lightsail.calls.clear()
    fake_deps.dns.requests.clear()
    code, out = remove(fake_deps)

    assert code == 0
    assert fake_deps.dns.calls() == [("DELETE", f"/{node.dns_record_ids['AAAA']}")]
    # the instance and its IP are gone: only a look for an instance named after the alias
    assert fake_deps.lightsail.calls == [("instance_tagged", "jp-1", "jp-1")]
    assert fake_deps.node() is None


def test_the_inventory_entry_that_cannot_be_deleted_is_reported(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.cloudflare.deny("DELETE", "/accounts/acct-1/storage/kv/namespaces/ns-1/values/node:jp-1", 500)

    code, out = remove(fake_deps)

    assert code == 1
    assert "控制端" in out and "没有删干净" in out
    assert fake_deps.node() is not None
    assert fake_deps.dns.records == {}
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_progress_is_saved_when_something_interrupts_the_steps(fake_deps, monkeypatch, interruption):
    deploy_aws(fake_deps)

    def interrupted(cf, zone_id, node):
        raise interruption()

    monkeypatch.setattr(rm, "delete_node_records", interrupted)
    with pytest.raises(interruption):
        remove(fake_deps)

    node = fake_deps.node()
    assert node is not None and set(node.dns_record_ids) == {"A", "AAAA"}
    assert (node.instance_name, node.static_ip_name) == (None, None)  # the AWS part is done and recorded
    assert not entry_deleted(fake_deps)


def test_a_progress_save_that_fails_does_not_hide_the_interruption(fake_deps, monkeypatch):
    deploy_aws(fake_deps)
    monkeypatch.setattr(rm, "delete_node_records", lambda cf, zone_id, node: (_ for _ in ()).throw(KeyboardInterrupt()))
    fake_deps.cloudflare.deny("PUT", "/accounts/acct-1/storage/kv/namespaces/ns-1/values/node:jp-1", 500)
    with pytest.raises(KeyboardInterrupt):
        remove(fake_deps)
    assert "没能把删除进度写入控制端" in fake_deps.terminal.text


@pytest.mark.parametrize("status", [401, 403])
def test_a_revoked_saved_token_says_how_to_enter_a_new_one(fake_deps, status):
    deploy_aws(fake_deps)
    fake_deps.cloudflare.deny("GET", "/zones", status)
    code, out = remove(fake_deps)
    assert code == 1
    assert str(fake_deps.deps.conf_path) in out and "删除 CLOUDFLARE_API_TOKEN" in out and f"HTTP {status}" in out
    assert fake_deps.node() is not None


def test_an_error_goes_to_stderr(fake_deps):
    code, _ = remove(fake_deps, "x")
    assert code == 1
    assert fake_deps.terminal.err.getvalue() == "错误：没有名为 x 的节点（这个 Cloudflare 账号里还没有控制端）。\n"
    assert "错误" not in fake_deps.terminal.out.getvalue()
