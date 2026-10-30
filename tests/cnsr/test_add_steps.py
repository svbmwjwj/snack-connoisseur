"""Tests for the steps of the ``cnsr up`` pipeline, one behaviour each."""

from __future__ import annotations

import re

import pytest
from deploy_fakes import AWS_IP, AWS_IPV6, VPS_IP, agent_key, preflight_output, run_up

from cnsr.cloudflare import control
from cnsr.cloudflare.control import ProbeResult
from cnsr.deploy import steps_sni
from cnsr.deploy.steps_node import RESTART_MARKER
from cnsr.errors import CnsrError
from cnsr.scan.egress import Egress
from cnsr.scan.probe import Hit
from cnsr.scan.select import parse_backups
from cnsr.server.xray import BACKUPS_PATH, COMPOSE_PATH, CONFIG_PATH, NODE_ENV_PATH, VERIFY_ENV_PATH, read_identity
from cnsr.ssh.config import read_block
from cnsr.ssh.remote import SshError

VPS_ANSWERS = ["", "", "已保存"]
DOWN = ProbeResult(ok=False, ms=None, error="连接超时")


def test_aws_creates_instance_and_forgets_old_host_key(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.lightsail.names() == [
        "pick", "static_ips", "static_ip_attachment", "ensure_key_pair", "ensure_instance", "wait_running",
        "ensure_static_ip", "instance_ipv6", "set_firewall"]  # fmt: skip
    assert fake_deps.lightsail.created == {"jp-1": "nano_3_0"}
    assert fake_deps.lightsail.calls[6] == ("ensure_static_ip", "jp-1", "jp-1-ip")
    node = fake_deps.node()
    assert (node.instance_name, node.static_ip_name, node.ipv4, node.ipv6) == ("jp-1", "jp-1-ip", AWS_IP, AWS_IPV6)
    assert fake_deps.calls["forget"] == [(AWS_IP, 22)]
    # the whole deploy goes by IP; only the final alias check logs in through the domain
    by_name = [entry for entry in fake_deps.server.log if entry[0] and entry[2] != AWS_IP]
    assert by_name == [("admin", 22, node.domain, "true")]
    assert {(user, port) for user, port, _, _ in fake_deps.server.log if user} == {("admin", 22)}

    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.calls["forget"] == [(AWS_IP, 22)]  # the instance was already known


def test_the_static_ip_name_is_recorded_only_once_the_ip_is_cnsrs(fake_deps):
    fake_deps.lightsail.ips["jp-1-ip"] = "web"  # a stranger's, attached to another instance
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "绑在实例 web 上" in fake_deps.terminal.text
    # the cleanup deletes the instance this run created, and leaves the stranger's IP alone
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {"jp-1-ip": "web"}
    assert ("release_static_ip", "jp-1-ip") not in fake_deps.lightsail.calls and fake_deps.node() is None


def test_aws_baseline_findings_stop_before_firewall(fake_deps):
    fake_deps.server.sshd = "port 22\npasswordauthentication yes\nkbdinteractiveauthentication no\n"
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "SSH 允许密码登录" in fake_deps.terminal.text
    assert "set_firewall" not in fake_deps.lightsail.names()
    assert fake_deps.calls["scan"] == []


def test_aws_baseline_sees_a_match_block_for_root(fake_deps):
    fake_deps.server.sshd_root = "port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin yes\n"
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "SSH 允许 root 用密码登录（root 登录时）" in fake_deps.terminal.text
    assert "set_firewall" not in fake_deps.lightsail.names()


def test_vps_installs_key_with_password_when_key_rejected(fake_deps):
    fake_deps.server.key_installed = False
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert fake_deps.calls["install_key"] == [("root", VPS_IP, 22, agent_key("jp-1").line)]
    assert "root 密码" in fake_deps.terminal.text
    assert fake_deps.server.did("preflight.sh")


def test_vps_root_is_hardened_and_alias_moves_to_admin(fake_deps):
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    stage = next(entry for entry in fake_deps.server.log if entry[3] == "harden-stage.sh")
    assert stage[:2] == ("root", 22)
    node = fake_deps.node()
    assert node.ssh.user == "admin" and node.ssh.port != 22
    docker = next(entry for entry in fake_deps.server.log if entry[3] == "install-docker.sh")
    assert docker[:3] == ("admin", node.ssh.port, VPS_IP)
    block = read_block(fake_deps.deps.ssh_config, "jp-1")
    assert (block.user, block.port, block.hostname) == ("admin", node.ssh.port, node.domain)


PROXIED, DIRECT = Egress("203.0.113.5", "198.51.100.9"), Egress("203.0.113.5", "203.0.113.5")


def test_the_node_says_where_this_computer_comes_from_before_the_scan(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    asked = [entry for entry in fake_deps.server.log if entry[3] == "printenv SSH_CLIENT"]
    assert asked == [("admin", 22, AWS_IP, "printenv SSH_CLIENT")]  # by IP, the way the scan goes
    assert len(fake_deps.calls["egress"]) == 1


def test_a_proxied_route_to_the_node_waits_until_the_proxy_is_off(fake_deps):
    fake_deps.egress_result = [PROXIED, DIRECT]
    fake_deps.terminal.inputs = ["1"]  # 已关闭代理，重新检查
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    text = fake_deps.terminal.text
    assert "节点看到你从 198.51.100.9 连过来" in text and "关闭代理" in text
    assert fake_deps.lightsail.names().count("ensure_instance") == 1 and len(fake_deps.calls["scan"]) == 1


def test_fake_ip_dns_stops_the_scan_even_when_the_addresses_agree(fake_deps):
    # A proxy that also carries the domestic echo shows its own address on both sides.
    fake_deps.egress_result = [Egress("203.0.113.77", "203.0.113.77", fake_dns=True), DIRECT]
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert "198.18.0.0/15" in fake_deps.terminal.text and len(fake_deps.calls["egress"]) == 2


def test_fake_ip_dns_with_no_echo_is_not_left_to_a_confirmation(fake_deps):
    fake_deps.egress_result = Egress(None, "203.0.113.77", fake_dns=True)
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "确认是直连" not in fake_deps.terminal.text and fake_deps.calls["scan"] == []


def test_the_route_is_asked_again_while_it_stays_proxied(fake_deps):
    fake_deps.egress_result = [PROXIED, PROXIED, DIRECT]
    fake_deps.terminal.inputs = ["1", "1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(fake_deps.calls["egress"]) == 3 and len(fake_deps.calls["scan"]) == 1


@pytest.mark.parametrize("seen", [Egress(None, "203.0.113.5"), Egress("203.0.113.5", None)], ids=["no-echo", "no-ssh-client"])
def test_unknown_egress_asks_before_scanning(fake_deps, seen):
    fake_deps.egress_result = seen
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert fake_deps.calls["scan"] == []

    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(fake_deps.calls["scan"]) == 1


def test_scan_covers_the_neighbourhood_only(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    (targets,) = fake_deps.calls["scan"]
    assert len(targets) == 4096 - 1024 - 2 and AWS_IP not in targets


def test_no_candidates_stop(fake_deps):
    fake_deps.hits = []
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "没有找到" in fake_deps.terminal.text
    assert not fake_deps.server.did("validate-candidates.sh") and not fake_deps.server.did("install-docker.sh")


def test_new_node_takes_fastest_candidate_and_keeps_every_other_one(fake_deps):
    # Backups are checked again before each use, so all that passed are worth keeping.
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    identity = read_identity(fake_deps.server.files[CONFIG_PATH])
    assert identity["sni"] == "site0.example.com"
    backups = parse_backups(fake_deps.server.files[BACKUPS_PATH])
    assert [b.domain for b in backups] == [f"site{i}.example.com" for i in range(1, 12)]


def test_candidates_a_reality_server_cannot_borrow_are_dropped(fake_deps):
    fake_deps.server.reality_fails = {"site0.example.com", "site2.example.com"}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert read_identity(fake_deps.server.files[CONFIG_PATH])["sni"] == "site1.example.com"
    backups = [b.domain for b in parse_backups(fake_deps.server.files[BACKUPS_PATH])]
    assert "site0.example.com" not in backups and "site2.example.com" not in backups
    # The check runs the pinned Xray image, so Docker comes first.
    what = fake_deps.server.what()
    assert what.index("validate-candidates.sh") < what.index("install-docker.sh") < what.index("reality-candidates.sh")


def test_no_candidate_passing_the_reality_check_stops(fake_deps):
    fake_deps.server.reality_fails = set(fake_deps.server.latency)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert CONFIG_PATH not in fake_deps.server.files


def test_an_unfinished_node_whose_sni_no_longer_passes_picks_another(fake_deps):
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    first = read_identity(fake_deps.server.files[CONFIG_PATH])
    assert first["sni"] == "site0.example.com" and fake_deps.node().line is None

    del fake_deps.server.fail_on["verify-reality.sh"]
    fake_deps.server.reality_fails = {"site0.example.com"}
    assert run_up(fake_deps) == 0
    now = read_identity(fake_deps.server.files[CONFIG_PATH])
    assert now["sni"] == "site1.example.com" and now["dest_ip"] != first["dest_ip"]
    assert (now["uuid"], now["private_key"], now["short_id"]) == (first["uuid"], first["private_key"], first["short_id"])
    assert len(fake_deps.calls["scan"]) == 2  # not in use yet: scanned like a first deployment
    assert fake_deps.node().xray.sni == "site1.example.com"
    assert "site1.example.com" not in [b.domain for b in parse_backups(fake_deps.server.files[BACKUPS_PATH])]


def test_an_unfinished_node_keeps_an_sni_that_still_passes(fake_deps):
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    first = fake_deps.server.files[CONFIG_PATH]

    del fake_deps.server.fail_on["verify-reality.sh"]
    assert run_up(fake_deps) == 0
    assert fake_deps.server.files[CONFIG_PATH] == first


def test_backups_are_capped(fake_deps):
    fake_deps.hits = [Hit(f"203.0.113.{i}", f"many{i}.example.com", "Example CA", False) for i in range(40)]
    fake_deps.server.latency = {f"many{i}.example.com": 10 + i for i in range(40)}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    # the 30 the shortlist hands the node, less the one in use; never more than MAX_BACKUPS
    assert len(parse_backups(fake_deps.server.files[BACKUPS_PATH])) == 29 and steps_sni.MAX_BACKUPS == 30


def test_a_rerun_scans_again_only_below_ten_backups(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0  # eleven backups
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(fake_deps.calls["scan"]) == 1
    assert steps_sni.REFILL_BELOW == 10


def test_rerun_with_few_backups_keeps_current_sni(fake_deps):
    fake_deps.server.latency = {f"site{i}.example.com": 50 - i for i in range(4)}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    first = read_identity(fake_deps.server.files[CONFIG_PATH])["sni"]
    assert first == "site3.example.com"

    fake_deps.server.latency = {f"site{i}.example.com": 10 + i for i in range(6)}
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert len(fake_deps.calls["scan"]) == 2
    assert read_identity(fake_deps.server.files[CONFIG_PATH])["sni"] == first
    backups = [b.domain for b in parse_backups(fake_deps.server.files[BACKUPS_PATH])]
    assert backups == [f"site{i}.example.com" for i in (0, 1, 2, 4, 5)]
    assert fake_deps.node().xray.sni == first


def test_changed_config_restarts_xray_on_rerun(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    restart = f"docker compose -f {COMPOSE_PATH} restart"
    assert restart not in fake_deps.server.what()
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert restart not in fake_deps.server.what()  # same config: the container keeps running
    fake_deps.server.files[CONFIG_PATH] = fake_deps.server.files[CONFIG_PATH].replace('"loglevel"', '"logLevel"')
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert restart in fake_deps.server.what()


def test_node_files(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    files, modes = fake_deps.server.files, fake_deps.server.modes
    token = fake_deps.node().node_token
    assert token and files[NODE_ENV_PATH] == f"CNSR_CONTROL=https://cnsr-control.acct-sub.workers.dev\nCNSR_NODE_TOKEN={token}\n"
    assert (modes[NODE_ENV_PATH], modes[BACKUPS_PATH], modes[CONFIG_PATH], modes[COMPOSE_PATH]) == ("600", "600", "644", "644")
    assert fake_deps.server.did("docker run --rm -v /opt/cnsr/xray/conf:/usr/local/etc/xray:ro")


def test_the_reality_self_check_gets_its_values_in_a_root_only_file(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    xray = fake_deps.node().xray
    values = f"UUID={xray.uuid}\nPUBLIC_KEY={xray.public_key}\nSHORT_ID={xray.short_id}\nSNI={xray.sni}\n"
    assert fake_deps.server.verified == [([VERIFY_ENV_PATH], values, "600")]
    assert VERIFY_ENV_PATH not in fake_deps.server.files


def test_probe_waits_for_a_new_control_plane(fake_deps):
    fake_deps.probe_results[AWS_IP] = [DOWN, DOWN]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.calls["probe"][:3] == [AWS_IP] * 3
    assert fake_deps.sleeps.count(10) >= 2


def test_v4_unreachable_fails_without_registering(fake_deps):
    fake_deps.probe_results[AWS_IP] = [DOWN] * 100
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "连接超时" in fake_deps.terminal.text
    assert fake_deps.node() is None and NODE_ENV_PATH not in fake_deps.server.files


def test_public_dns_not_ready_stops_before_line(fake_deps):
    fake_deps.public_dns = False
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "稍后重跑" in fake_deps.terminal.text
    assert fake_deps.node() is None and NODE_ENV_PATH not in fake_deps.server.files


def test_alias_switch_timeout_keeps_ip_and_says_so(fake_deps):
    fake_deps.local_dns = False
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert read_block(fake_deps.deps.ssh_config, "jp-1").hostname == AWS_IP
    assert "cnsr up jp-1" in fake_deps.terminal.text and AWS_IP in fake_deps.terminal.text


def test_alias_switches_to_domain(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    node = fake_deps.node()
    assert read_block(fake_deps.deps.ssh_config, "jp-1").hostname == node.domain
    assert ("admin", 22, node.domain, "true") in fake_deps.server.log


@pytest.mark.parametrize("error", [CnsrError("握手验证失败"), KeyboardInterrupt()])
def test_failed_step_keeps_partial_node(fake_deps, error):
    # A vendor server keeps what a failed run did; a first AWS run cleans up (test_add_cleanup).
    fake_deps.server.fail_on["verify-reality.sh"] = error
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_up(fake_deps, ip=VPS_IP)
    else:
        assert run_up(fake_deps, ip=VPS_IP) == 1
        assert "握手验证失败" in fake_deps.terminal.text
    # The stage display is closed on every path, with the way it ended.
    word = "已中断" if isinstance(error, KeyboardInterrupt) else "失败"
    assert re.search(rf"^\[\d/8\] .*：{word}（\d+:\d\d）$", fake_deps.terminal.text, flags=re.M)
    node = fake_deps.node()
    assert node.domain and set(node.dns_record_ids) == {"A", "AAAA"}
    assert node.xray is not None and node.line is None


def test_v4_probe_refused_by_control_plane_fails(fake_deps):
    def probe(meta, host):
        raise CnsrError("控制端拒绝了管理 token，请重新运行 add 更新控制端（HTTP 401）。")

    fake_deps.deps.probe = probe
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "HTTP 401" in fake_deps.terminal.text and fake_deps.node() is None


def test_new_instance_gets_time_to_start_sshd(fake_deps):
    fake_deps.server.fail_on["true"] = SshError("port")

    def sshd_starts_while_waiting(seconds):
        fake_deps.sleeps.append(seconds)
        fake_deps.server.fail_on.pop("true", None)

    fake_deps.deps.sleep = sshd_starts_while_waiting
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.sleeps[0] == 10 and "等待 SSH 就绪" in fake_deps.terminal.text


def test_known_instance_login_failure_stops_at_once(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.server.fail_on["true"] = SshError("key")
    sleeps = len(fake_deps.sleeps)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "密钥被拒" in fake_deps.terminal.text and len(fake_deps.sleeps) == sleeps


def test_refused_takeover_after_password_login_says_what_changed(fake_deps):
    fake_deps.server.key_installed = False
    fake_deps.server.preflight = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"))
    fake_deps.terminal.inputs = ["", ""]
    assert run_up(fake_deps, ip=VPS_IP) == 1
    text = fake_deps.terminal.text
    assert "没有建解析" in text and "authorized_keys" in text


def test_vps_record_saved_once_taken_over(fake_deps):
    fake_deps.server.fail_on["install-docker.sh"] = CnsrError("Docker 安装失败")
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    node = fake_deps.node()
    assert node.domain and node.ssh.user == "admin" and node.xray is None


def test_interrupted_restart_is_finished_on_rerun(fake_deps):
    restart = f"docker compose -f {COMPOSE_PATH} restart"
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    fake_deps.server.files[CONFIG_PATH] = fake_deps.server.files[CONFIG_PATH].replace('"loglevel"', '"logLevel"')
    fake_deps.server.fail_on[restart] = CnsrError("重启失败")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert RESTART_MARKER in fake_deps.server.files and '"logLevel"' not in fake_deps.server.files[CONFIG_PATH]

    del fake_deps.server.fail_on[restart]
    logged = len(fake_deps.server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0  # the config is unchanged now, but Xray never loaded it
    rerun = fake_deps.server.what()[logged:]
    assert restart in rerun and rerun.index(restart) < rerun.index("verify-reality.sh")
    assert RESTART_MARKER not in fake_deps.server.files

    logged = len(fake_deps.server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert restart not in fake_deps.server.what()[logged:]


def test_rerun_without_identity_in_inventory_derives_public_key_locally(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    first = fake_deps.node()
    first_xray, first.xray = first.xray, None
    fake_deps.store(first)
    logged = len(fake_deps.server.log)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.node().xray == first_xray
    assert not any("x25519" in what for what in fake_deps.server.what()[logged:])


def test_add_registers_the_node_token_and_private_key(fake_deps, monkeypatch):
    from cnsr import runlog

    registered = []
    monkeypatch.setattr(runlog, "secret", registered.append)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    seen = list(registered)  # snapshot first: reading the node back below runs Node.__post_init__ too
    (private_key,) = fake_deps.server.generated
    assert fake_deps.node().node_token in seen
    assert private_key in seen
    registered.clear()
    assert run_up(fake_deps, aws="jp-tokyo") == 0  # rerun: the key is read back from the node
    assert private_key in registered


def test_a_challenged_control_plane_stops_before_anything_is_created(fake_deps):
    def blocked(meta):
        raise control.ControlPlaneBlocked("c.example.com", "challenge")

    fake_deps.deps.check_control = blocked
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "Cloudflare 拦下了" in fake_deps.terminal.screen
    assert fake_deps.lightsail.calls == [] and fake_deps.node() is None


def test_a_challenge_seen_by_the_probe_is_not_retried(fake_deps):
    def probe(meta, host):
        raise control.ControlPlaneBlocked("c.example.com", "challenge")

    fake_deps.deps.probe = probe
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert fake_deps.sleeps.count(10) == 0 and "Cloudflare 拦下了" in fake_deps.terminal.screen
