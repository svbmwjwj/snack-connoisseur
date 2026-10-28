"""``cnsr up`` must never leave cnsr unable to reach a node, nor repoint someone else's alias.

Hardening a vendor server moves SSH to admin on a new port. That login must survive a control
plane that cannot be written, Ctrl-C and a rerun; and an alias that already names another
machine is never taken over.
"""

from __future__ import annotations

import pytest
from deploy_fakes import SSHD_CLEAN, VPS_IP, run_up

from cnsr.deploy import steps_host
from cnsr.server.harden import DROP_IN, Hardener
from cnsr.ssh.config import read_block

VPS_ANSWERS = ["", "", "已保存"]
NODE_KEY = "/accounts/acct-1/storage/kv/namespaces/ns-1/values/node:jp-1"
CUT_ALIAS = "Host jp-1\n    HostName 203.0.113.9\n    User ops\n"


def deploy_vps(fake_deps) -> tuple[str, int]:
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    node = fake_deps.node()
    return node.ssh.user, node.ssh.port


def forget_new_login(fake_deps) -> None:
    """The record as it is when the control plane never heard of the hardening."""
    node = fake_deps.node()
    node.ssh.user, node.ssh.port = "root", 22
    fake_deps.store(node)


def control_plane_fails_after_hardening(fake_deps, monkeypatch) -> None:
    harden = Hardener.run

    def run(self, *args, **kwargs):
        result = harden(self, *args, **kwargs)
        fake_deps.cloudflare.deny("PUT", NODE_KEY, 500)
        return result

    monkeypatch.setattr(steps_host.Hardener, "run", run)


# --- the new login is kept --------------------------------------------------------------


def test_alias_is_written_and_the_save_retried_before_giving_up_with_the_new_login(fake_deps, monkeypatch):
    control_plane_fails_after_hardening(fake_deps, monkeypatch)
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    block = read_block(fake_deps.deps.ssh_config, "jp-1")
    assert block.user == "admin" and block.port != 22
    text = fake_deps.terminal.text
    assert f"用户 admin、端口 {block.port}" in text and "试了 3 次" in text
    assert fake_deps.sleeps.count(steps_host.SAVE_RETRY_SECONDS) == 2
    assert fake_deps.node().ssh.user == "root"  # the control plane never heard of it


def test_a_rerun_logs_in_the_way_the_alias_says_and_records_it(fake_deps, monkeypatch):
    control_plane_fails_after_hardening(fake_deps, monkeypatch)
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    port = read_block(fake_deps.deps.ssh_config, "jp-1").port
    fake_deps.cloudflare.allow("PUT", NODE_KEY)
    fake_deps.server.refused[("root", 22)] = "port"  # 22 is closed now
    logged = len(fake_deps.server.log)

    assert run_up(fake_deps, ip=VPS_IP) == 0

    node = fake_deps.node()
    assert (node.ssh.user, node.ssh.port) == ("admin", port)
    rerun = fake_deps.server.log[logged:]
    assert rerun[0][:2] == ("root", 22) and rerun[1][:2] == ("admin", port)
    assert not any(what == "harden-stage.sh" for *_, what in rerun)
    assert fake_deps.calls["install_key"] == []


def test_the_alias_of_another_host_is_not_tried(fake_deps):
    deploy_vps(fake_deps)
    forget_new_login(fake_deps)
    fake_deps.deps.ssh_config.write_text(CUT_ALIAS.replace("ops", "admin"))
    fake_deps.server.refused[("root", 22)] = "port"
    logged = len(fake_deps.server.log)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    assert "从外部也连不上 198.51.100.7 的 22 端口" in fake_deps.terminal.text  # it refuses everyone
    assert [entry[:2] for entry in fake_deps.server.log[logged:]] == [("root", 22)]


def test_ctrl_c_after_the_cut_is_proven_keeps_the_new_login(fake_deps):
    fake_deps.server.fail_on["systemctl stop"] = KeyboardInterrupt()
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    with pytest.raises(KeyboardInterrupt):
        run_up(fake_deps, ip=VPS_IP)
    node = fake_deps.node()
    assert node.ssh.user == "admin" and node.ssh.port != 22


def test_a_rerun_finishes_a_hardening_the_timer_undid(fake_deps):
    user, port = deploy_vps(fake_deps)
    server = fake_deps.server
    server.files[DROP_IN] = f"Port 22\nPort {port}\n"
    server.sshd = f"port 22\nport {port}\n" + SSHD_CLEAN  # the revert timer brought 22 back
    logged = len(server.log)

    assert run_up(fake_deps, ip=VPS_IP) == 0

    rerun = [what for *_, what in server.log[logged:]]
    assert "harden-cut.sh" in rerun and "harden-stage.sh" not in rerun
    stops = [i for i, what in enumerate(rerun) if what == "systemctl stop cnsr-ssh-revert.timer"]
    # the fired timer is gone (stop exits 5); the cut arms a new one, stopped after the fresh login
    assert stops[0] < rerun.index("harden-cut.sh") < stops[1] and server.timer == "unloaded"
    assert server.sshd.startswith(f"port {port}\n") and "补完" in fake_deps.terminal.text
    assert (fake_deps.node().ssh.user, fake_deps.node().ssh.port) == (user, port)


def test_a_finished_hardening_is_only_checked_on_rerun(fake_deps):
    _, port = deploy_vps(fake_deps)
    assert fake_deps.server.timer == "unloaded"  # stopped by that run: `systemctl stop` now exits 5
    fake_deps.server.files[DROP_IN] = f"Port {port}\n"
    logged = len(fake_deps.server.log)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    rerun = [what for *_, what in fake_deps.server.log[logged:]]
    assert "systemctl stop cnsr-ssh-revert.timer" in rerun and "harden-cut.sh" not in rerun
    assert any(what.startswith("sshd -T -C user=root,") for what in rerun)
    assert "已经由 cnsr 加固过" in fake_deps.terminal.text


def test_a_timer_that_cannot_be_stopped_ends_the_rerun_without_changes(fake_deps):
    user, port = deploy_vps(fake_deps)
    server = fake_deps.server
    server.files[DROP_IN] = f"Port 22\nPort {port}\n"
    server.sshd = f"port 22\nport {port}\n" + SSHD_CLEAN
    server.timer, server.timer_stop_fails = "active", True
    logged = len(server.log)

    assert run_up(fake_deps, ip=VPS_IP) == 1

    rerun = [what for *_, what in server.log[logged:]]
    assert "没能停止" in fake_deps.terminal.text and "没有做任何改动" in fake_deps.terminal.text
    assert "harden-cut.sh" not in rerun and not any(what.startswith("sshd -T") for what in rerun)
    assert server.sshd.startswith("port 22\n") and server.timer == "active"
    assert (fake_deps.node().ssh.user, fake_deps.node().ssh.port) == (user, port)


# --- an alias that names another machine ------------------------------------------------


def test_a_new_vps_node_does_not_take_an_alias_that_names_another_server(fake_deps):
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text(CUT_ALIAS)
    assert run_up(fake_deps, ip=VPS_IP) == 1
    text = fake_deps.terminal.text
    assert "Host jp-1" in text and "203.0.113.9" in text and "换一个别名" in text
    assert ssh_config.read_text() == CUT_ALIAS
    assert fake_deps.server.log == [] and fake_deps.node() is None
    assert fake_deps.terminal.prompts == []


def test_a_new_vps_node_without_an_ip_refuses_an_alias_that_names_a_host(fake_deps):
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text("Host jp-1\n    HostName box.example.com\n    User ops\n")
    fake_deps.terminal.inputs = ["1"]  # source: an existing server
    assert run_up(fake_deps) == 1
    assert "box.example.com" in fake_deps.terminal.text
    assert len(fake_deps.terminal.prompts) == 1  # the IP is never asked
    assert fake_deps.server.log == []


def test_a_new_aws_node_does_not_take_an_existing_alias(fake_deps):
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text(CUT_ALIAS)
    assert run_up(fake_deps, aws="jp") == 1
    assert "Host jp-1" in fake_deps.terminal.text and "203.0.113.9" in fake_deps.terminal.text
    assert fake_deps.lightsail.calls == [] and fake_deps.node() is None
    assert ssh_config.read_text() == CUT_ALIAS


def test_an_alias_without_a_hostname_is_no_obstacle(fake_deps):
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text("Host jp-1\n    ForwardAgent yes\n")
    assert run_up(fake_deps, aws="jp") == 0
    assert "ForwardAgent yes" in ssh_config.read_text()


def test_an_alias_of_the_requested_server_is_used(fake_deps):
    ssh_config = fake_deps.deps.ssh_config
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text(f"Host jp-1\n    HostName {VPS_IP}\n")
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert fake_deps.node().ipv4 == VPS_IP


# --- a free static IP of the node's name may be the user's -------------------------------


def test_a_free_static_ip_cnsr_did_not_allocate_is_used_only_after_you_agree(fake_deps):
    fake_deps.lightsail.ips["jp-1-ip"] = None
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp") == 0
    assert "静态 IP jp-1-ip" in fake_deps.terminal.seen[0] and len(fake_deps.terminal.prompts) == 1
    assert fake_deps.lightsail.ips == {"jp-1-ip": "jp-1"} and fake_deps.node().static_ip_name == "jp-1-ip"


def test_declining_the_free_static_ip_stops_before_any_instance_is_created(fake_deps):
    fake_deps.lightsail.ips["jp-1-ip"] = None
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp") == 1
    assert "保持原样" in fake_deps.terminal.text
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {"jp-1-ip": None}


def test_without_a_terminal_a_free_static_ip_is_refused(fake_deps):
    fake_deps.lightsail.ips["jp-1-ip"] = None
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="jp") == 1
    assert "没有确认" in fake_deps.terminal.text
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {"jp-1-ip": None}


def test_the_recorded_static_ip_is_reused_without_asking(fake_deps):
    assert run_up(fake_deps, aws="jp") == 0
    fake_deps.lightsail.ips["jp-1-ip"] = None  # detached, for example while the instance was recreated
    assert run_up(fake_deps, aws="jp") == 0
    assert fake_deps.terminal.prompts == [] and fake_deps.lightsail.ips == {"jp-1-ip": "jp-1"}
