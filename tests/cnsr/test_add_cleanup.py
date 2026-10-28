"""A first AWS deployment that fails or is interrupted deletes what it created, so nothing keeps costing.

A node that has been deployed before is in use and is left as it is, and so is a vendor server.
"""

from __future__ import annotations

import pytest
from deploy_fakes import VPS_IP, preflight_output, run_up

from cnsr.errors import CnsrError
from cnsr.ssh.config import read_block

REFUSED = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"), user="admin")
CLEANED = "已清理干净，不会继续计费"


def nothing_left(fakes) -> bool:
    return (
        fakes.lightsail.instances == {} and fakes.lightsail.ips == {} and fakes.dns.records == {}
        and fakes.node() is None and read_block(fakes.deps.ssh_config, "jp-1") is None
    )  # fmt: skip


def test_a_refused_takeover_deletes_the_new_instance_and_static_ip(fake_deps):
    fake_deps.server.preflight = REFUSED
    assert run_up(fake_deps, aws="jp") == 1
    assert nothing_left(fake_deps)
    assert "443 端口已被占用" in fake_deps.terminal.text and CLEANED in fake_deps.terminal.text


def test_a_late_failure_also_deletes_the_dns_records_and_the_alias(fake_deps):
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    assert run_up(fake_deps, aws="jp") == 1
    assert nothing_left(fake_deps) and "握手验证失败" in fake_deps.terminal.text


def test_ctrl_c_cleans_up_too(fake_deps):
    fake_deps.server.fail_on["verify-reality.sh"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_up(fake_deps, aws="jp")
    assert nothing_left(fake_deps) and CLEANED in fake_deps.terminal.text


def test_an_unexpected_error_cleans_up_and_goes_on_up(fake_deps):
    fake_deps.server.fail_on["verify-reality.sh"] = RuntimeError("a bug")
    with pytest.raises(RuntimeError, match="a bug"):
        run_up(fake_deps, aws="jp")
    assert nothing_left(fake_deps) and CLEANED in fake_deps.terminal.text


def test_a_static_ip_allocated_but_not_attached_is_released_too(fake_deps):
    fake_deps.lightsail.attach_error = CnsrError("绑定静态 IP 失败：Throttling")
    assert run_up(fake_deps, aws="jp") == 1
    assert ("release_static_ip", "jp-1-ip") in fake_deps.lightsail.calls
    assert nothing_left(fake_deps) and CLEANED in fake_deps.terminal.text


def test_the_instance_is_on_record_before_it_exists(fake_deps, monkeypatch):
    # A run killed outright right after the create still leaves rm something to find.
    seen = []
    create = fake_deps.lightsail.ensure_instance

    def ensure_instance(name, key_pair):
        node = fake_deps.node()
        seen.append(node and node.instance_name)
        return create(name, key_pair)

    monkeypatch.setattr(fake_deps.lightsail, "ensure_instance", ensure_instance)
    assert run_up(fake_deps, aws="jp") == 0
    assert seen == ["jp-1"]


def test_the_cleanup_is_reported_after_the_error(fake_deps):
    fake_deps.server.preflight = REFUSED
    run_up(fake_deps, aws="jp")
    assert fake_deps.terminal.err.getvalue().startswith("错误：")
    assert fake_deps.terminal.out.getvalue().rstrip().endswith(CLEANED + "。修好问题后重跑同一条命令会重新开一台。")


def test_a_rerun_after_a_cleanup_starts_from_scratch(fake_deps):
    fake_deps.server.preflight = REFUSED
    assert run_up(fake_deps, aws="jp") == 1
    fake_deps.server.preflight = preflight_output(user="admin")
    fake_deps.lightsail.calls.clear()
    assert run_up(fake_deps, aws="jp") == 0
    assert ("ensure_instance", "jp-1", "jp-1") in fake_deps.lightsail.calls and fake_deps.node().line


def test_what_could_not_be_deleted_is_listed(fake_deps):
    fake_deps.server.preflight = REFUSED
    fake_deps.lightsail.delete_instance_errors = ["删除实例 jp-1 失败：AccessDenied"]
    assert run_up(fake_deps, aws="jp") == 1
    text = fake_deps.terminal.text
    assert "删除实例 jp-1 失败：AccessDenied" in text and "cnsr rm jp-1" in text and CLEANED not in text
    assert fake_deps.node().instance_name == "jp-1"  # the record stays for rm to finish


def test_a_failure_before_the_instance_exists_removes_the_record(fake_deps):
    def refuse(name, pub_line):
        raise CnsrError("导入钥匙失败")

    fake_deps.lightsail.ensure_key_pair = refuse
    assert run_up(fake_deps, aws="jp") == 1
    assert nothing_left(fake_deps) and "导入钥匙失败" in fake_deps.terminal.text


def test_a_failed_rerun_of_a_deployed_node_deletes_nothing(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    assert run_up(fake_deps, aws="jp") == 1
    assert fake_deps.lightsail.instances == {"jp-1": True} and fake_deps.node().line
    assert "清理" not in fake_deps.terminal.text


def test_a_vendor_server_is_never_cleaned_up(fake_deps):
    fake_deps.server.fail_on["install-docker.sh"] = CnsrError("Docker 安装失败")
    fake_deps.terminal.inputs = ["", "", "已保存"]
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert fake_deps.node() is not None and "清理" not in fake_deps.terminal.text


def test_a_first_deployment_that_fails_at_the_rotate_service_is_cleaned_up(fake_deps, monkeypatch):
    server = fake_deps.server
    server.service_fails = True
    original = server._rotate_service

    def first_contact(verb):  # the service reaches the control plane before it dies
        fake_deps.kv()["state:jp-1"] = '{"version": 1, "exhausted": false}'
        return original(verb)

    monkeypatch.setattr(server, "_rotate_service", first_contact)
    assert run_up(fake_deps, aws="jp") == 1
    assert "换 SNI 服务没有在节点上运行起来" in fake_deps.terminal.text and CLEANED in fake_deps.terminal.text
    assert nothing_left(fake_deps)
    assert not any(key.startswith(("node:", "state:")) for key in fake_deps.kv())
