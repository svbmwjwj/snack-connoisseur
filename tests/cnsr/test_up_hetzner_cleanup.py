"""A first Hetzner deployment that fails deletes its server, Primary IP and firewall; ``cnsr rm``
deletes them too, each only when it carries cnsr's label for the node."""

from __future__ import annotations

import pytest
from deploy_fakes import preflight_output, run_rm, run_up

from cnsr import features
from cnsr.errors import CnsrError
from cnsr.ssh.config import read_block

REFUSED = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"))
CLEANED = "已清理干净，不会继续计费"


@pytest.fixture
def hz(fake_deps):
    fake_deps.hcloud_env = "hz-env-token-0001"
    fake_deps.terminal.inputs = ["已保存"]
    return fake_deps


def deploy(fakes, chosen=features.ALL) -> int:
    return run_up(fakes, hetzner="sg", size="1c1g", features=list(chosen))


def nothing_left(fakes) -> bool:
    hetzner = fakes.hetzner
    return (
        hetzner.servers == {} and hetzner.ips == {} and hetzner.firewalls == {} and fakes.dns.records == {}
        and fakes.node() is None and read_block(fakes.deps.ssh_config, "jp-1") is None
    )  # fmt: skip


# --- the cleanup after a failed first deployment -------------------------------------------


def test_a_refused_takeover_deletes_the_server_and_its_primary_ip(hz):
    hz.server.preflight = REFUSED
    assert deploy(hz) == 1
    assert nothing_left(hz) and CLEANED in hz.terminal.text
    assert "删除这次在 Hetzner 和 Cloudflare 上建的服务器、Primary IP、防火墙" in hz.terminal.text


def test_a_late_failure_also_deletes_the_firewall_dns_records_and_alias(hz):
    hz.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    assert deploy(hz) == 1
    names = hz.hetzner.names()
    assert names.index("delete_instance") < names.index("delete_primary_ip") < names.index("delete_firewall")
    assert nothing_left(hz) and "握手验证失败" in hz.terminal.text


def test_ctrl_c_during_hardening_cleans_up_too(hz):
    hz.server.fail_on["harden-stage.sh"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        deploy(hz)
    assert nothing_left(hz) and CLEANED in hz.terminal.text


def test_out_of_stock_at_create_deletes_the_primary_ip(hz):
    hz.hetzner.out_of_stock = CnsrError("Hetzner sg（新加坡）创建 cpx02 失败：这个型号暂时无货或不能购买")
    assert deploy(hz) == 1
    assert "暂时无货" in hz.terminal.text
    assert nothing_left(hz) and CLEANED in hz.terminal.text


def test_a_failure_before_the_primary_ip_exists_removes_the_record(hz):
    def refuse(name, pub_line):
        raise CnsrError("上传 SSH key 失败")

    hz.hetzner.ensure_key_pair = refuse
    assert deploy(hz) == 1
    assert nothing_left(hz) and "上传 SSH key 失败" in hz.terminal.text


def test_a_stranger_server_stops_the_run_before_anything_is_created_or_recorded(hz):
    hz.hetzner.servers["jp-1"] = False
    assert deploy(hz) == 1
    text = hz.terminal.text
    assert hz.hetzner.servers == {"jp-1": False} and hz.hetzner.ips == {}
    assert "已有同名服务器 jp-1，但它不是 cnsr 创建的" in text and "请换一个别名" in text
    assert "ensure_primary_ip" not in hz.hetzner.names() and ("delete_instance", "jp-1") not in hz.hetzner.calls
    assert hz.node() is None and CLEANED in text  # no record that rm could never delete


def test_a_rerun_against_a_stranger_server_creates_no_primary_ip_either(hz):
    hz.hetzner.servers["jp-1"] = False
    assert deploy(hz) == 1
    hz.hetzner.calls.clear()
    assert deploy(hz) == 1
    assert "ensure_primary_ip" not in hz.hetzner.names() and hz.hetzner.ips == {}
    assert hz.node() is None


def test_what_could_not_be_deleted_is_listed_and_kept_on_record(hz):
    hz.server.preflight = REFUSED
    hz.hetzner.delete_instance_errors = ["Hetzner API 请求失败（HTTP 423）：资源被锁定"]
    assert deploy(hz) == 1
    text = hz.terminal.text
    assert "HTTP 423" in text and "cnsr rm jp-1" in text and CLEANED not in text
    node = hz.node()
    assert (node.instance_name, node.static_ip_name) == ("jp-1", "jp-1-ip")
    assert "delete_primary_ip" not in hz.hetzner.names()  # still assigned to the server


def test_a_failed_rerun_of_a_deployed_node_deletes_nothing(hz):
    assert deploy(hz) == 0
    hz.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    assert run_up(hz, hetzner="sg") == 1
    assert hz.hetzner.servers == {"jp-1": True} and hz.node().line
    assert "清理" not in hz.terminal.text


def test_a_rerun_after_a_cleanup_starts_from_scratch(hz):
    hz.server.preflight = REFUSED
    assert deploy(hz) == 1
    hz.server.preflight = preflight_output()
    hz.hetzner.calls.clear()
    hz.terminal.inputs = ["已保存"]
    assert deploy(hz) == 0
    assert ("ensure_instance", "jp-1", "jp-1", "cpx02", "jp-1-ip") in hz.hetzner.calls and hz.node().line


# --- cnsr rm -------------------------------------------------------------------------------


def test_rm_deletes_the_server_primary_ip_and_firewall(hz):
    assert deploy(hz) == 0
    hz.terminal.inputs = ["1"]
    assert run_rm(hz) == 0
    screen = hz.terminal.screen
    assert "Hetzner 服务器 jp-1（区域 sg" in screen and "Primary IP jp-1-ip" in screen and "防火墙 jp-1-fw" in screen
    assert "Hetzner 上的 SSH key" in screen and "删除 Hetzner 服务器、Primary IP 和防火墙" in screen
    assert nothing_left(hz)


def test_rm_without_a_token_keeps_the_record(hz):
    assert deploy(hz) == 0
    hz.hcloud_env = None
    hz.terminal.inputs = ["1"]
    assert run_rm(hz) == 1
    assert "没有找到 Hetzner token" in hz.terminal.text
    assert hz.node() is not None and hz.hetzner.servers == {"jp-1": True}


def test_rm_finds_a_primary_ip_the_record_lacks_by_its_name_and_label(hz):
    assert deploy(hz) == 0
    node = hz.node()
    node.instance_name = node.static_ip_name = None
    hz.store(node)
    hz.hetzner.delete_instance("jp-1")  # gone by hand; leave a free IP of cnsr's behind
    hz.hetzner.ips["jp-1-ip"], hz.hetzner.assigned["jp-1-ip"] = True, None
    hz.terminal.inputs = ["1"]
    assert run_rm(hz) == 0
    assert ("delete_primary_ip", "jp-1-ip", "jp-1") in hz.hetzner.calls and nothing_left(hz)
