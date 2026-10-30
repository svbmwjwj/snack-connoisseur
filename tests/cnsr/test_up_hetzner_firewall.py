"""The Hetzner cloud firewall around hardening: the new SSH port is let in before the server
listens on it, and 22 is dropped only once hardening has succeeded and the login is stored."""

from __future__ import annotations

import random

import pytest
from deploy_fakes import run_up

from cnsr import features
from cnsr.errors import CnsrError
from cnsr.server.harden import DROP_IN, Hardener

XRAY_PORTS = ((443, "tcp"), (443, "udp"))


@pytest.fixture
def hz(fake_deps):
    """The fakes with a Hetzner token, whose firewall calls also go into the server's log."""
    fake_deps.hcloud_env = "hz-env-token-0001"
    fake_deps.terminal.inputs = ["已保存"]
    hetzner, log = fake_deps.hetzner, fake_deps.server.log
    set_rules, attach = hetzner.set_rules, hetzner.attach_firewall

    def logged_set_rules(name, ssh_ports, app_ports):
        log.append(("", 0, "", f"set_rules {tuple(ssh_ports)}"))
        set_rules(name, ssh_ports, app_ports)

    def logged_attach(name, server):
        log.append(("", 0, "", "attach_firewall"))
        attach(name, server)

    hetzner.set_rules, hetzner.attach_firewall = logged_set_rules, logged_attach
    return fake_deps


def deploy(fakes, chosen=features.ALL) -> int:
    return run_up(fakes, hetzner="sg", size="1c1g", features=list(chosen))


def rules(fakes) -> list[tuple]:
    return [call[2:] for call in fakes.hetzner.calls if call[0] == "set_rules"]


def firewall_events(fakes) -> list[str]:
    """The firewall calls and hardening scripts, in the order they happened."""
    wanted = ("set_rules", "attach_firewall", "harden-stage.sh", "harden-cut.sh", "harden-revert.sh")
    return [what for what in fakes.server.what() if what.startswith(wanted)]


# --- a first deployment --------------------------------------------------------------------


def test_the_firewall_opens_22_then_the_new_port_then_drops_22(hz):
    assert deploy(hz) == 0
    port = hz.node().ssh.port
    assert firewall_events(hz) == [
        "set_rules (22,)", "attach_firewall", f"set_rules (22, {port})", "harden-stage.sh", "harden-cut.sh",
        f"set_rules ({port},)"]  # fmt: skip
    assert rules(hz) == [((22,), XRAY_PORTS), ((22, port), XRAY_PORTS), ((port,), XRAY_PORTS)]
    assert hz.hetzner.rules["jp-1-fw"] == ((port,), XRAY_PORTS)
    assert hz.hetzner.attached == {"jp-1-fw": "jp-1"}
    assert ("ensure_firewall", "jp-1-fw", "jp-1") in hz.hetzner.calls


def test_the_firewall_is_applied_only_in_step_3(hz):
    assert deploy(hz) == 0
    what = hz.server.what()
    assert what.index("preflight.sh") < what.index("attach_firewall")


def test_harden_only_opens_ssh_alone(hz):
    assert deploy(hz, [features.HARDEN]) == 0
    port = hz.node().ssh.port
    assert rules(hz) == [((22,), ()), ((22, port), ()), ((port,), ())]


def test_22_is_dropped_only_after_the_new_login_is_stored(hz, monkeypatch):
    seen = []
    set_rules = hz.hetzner.set_rules

    def recording(name, ssh_ports, app_ports):
        if 22 not in ssh_ports:
            node = hz.node()
            seen.append((node.ssh.user, node.ssh.port))
        set_rules(name, ssh_ports, app_ports)

    monkeypatch.setattr(hz.hetzner, "set_rules", recording)
    assert deploy(hz) == 0
    assert seen == [("admin", hz.node().ssh.port)]


# --- failures keep 22 open -----------------------------------------------------------------


def test_a_failed_stage_rolls_back_and_keeps_22_open(hz):
    hz.server.fail_on["harden-stage.sh"] = CnsrError("stage 失败")
    assert deploy(hz) == 1
    assert hz.server.did("harden-revert.sh")
    assert all(22 in ssh_ports for ssh_ports, _ in rules(hz))
    assert len(rules(hz)) == 2 and rules(hz)[-1][0][0] == 22


def test_a_failure_after_the_cut_leaves_22_and_the_new_port_open_for_the_timer(hz):
    hz.server.fail_on["harden-cut.sh"] = CnsrError("cut 失败")
    assert deploy(hz) == 1
    assert "自动恢复" in hz.terminal.text
    (_, staged) = rules(hz)
    assert staged[0][0] == 22 and len(staged[0]) == 2  # 22 and the new port, never dropped


def test_a_firewall_that_cannot_open_the_new_port_stops_before_the_server_changes(hz, monkeypatch):
    set_rules = hz.hetzner.set_rules

    def refuse_new_port(name, ssh_ports, app_ports):
        if tuple(ssh_ports) != (22,):
            raise CnsrError("Hetzner API 请求失败（HTTP 503）：Hetzner 暂时不可用，请稍后重试")
        set_rules(name, ssh_ports, app_ports)

    monkeypatch.setattr(hz.hetzner, "set_rules", refuse_new_port)
    assert deploy(hz) == 1
    assert not hz.server.did("harden-stage.sh") and not hz.server.did("harden-revert.sh")
    assert "HTTP 503" in hz.terminal.text


def test_the_hardener_calls_on_port_before_it_stages(monkeypatch):
    events = []

    class Root:
        def run_script(self, name, args):
            events.append(name)
            raise CnsrError("stop here")

    class Ui:
        def detail(self, text):
            pass

        def warn(self, text):
            pass

    hardener = Hardener(Root(), lambda user, port: None, Ui(), random.Random(1))
    with pytest.raises(CnsrError):
        hardener.run("ssh-ed25519 AAAA k", set(), app_ports=(), on_port=lambda port: events.append(port))
    assert isinstance(events[0], int) and events[1] == "harden-stage.sh"


# --- reruns --------------------------------------------------------------------------------


def test_adding_xray_later_opens_443_in_the_firewall_and_in_ufw(hz):
    assert deploy(hz, [features.HARDEN]) == 0
    port = hz.node().ssh.port
    hz.server.files[DROP_IN] = ""  # what harden-stage.sh leaves on a real server
    hz.hetzner.calls.clear()
    assert run_up(hz, hetzner="sg", features=list(features.ALL)) == 0
    assert rules(hz) == [((port,), XRAY_PORTS)]
    assert {"443/tcp", "443/udp"} <= hz.server.ufw_allowed
    what = hz.server.what()
    assert what.index("ufw allow 443/tcp") > len(what) - 1 - what[::-1].index(f"set_rules ({port},)")


def test_a_rerun_that_skips_hardening_still_sets_the_rules(hz):
    assert deploy(hz, [features.HARDEN]) == 0  # no drop-in on this fake server: the rerun skips hardening
    port = hz.node().ssh.port
    hz.hetzner.calls.clear()
    assert run_up(hz, hetzner="sg", features=list(features.ALL)) == 0
    assert rules(hz) == [((port,), XRAY_PORTS)] and ("attach_firewall", "jp-1-fw", "jp-1") in hz.hetzner.calls


def test_a_rerun_still_logging_in_as_root_keeps_22(hz):
    hz.server.fail_on["harden-stage.sh"] = CnsrError("stage 失败")
    hz.hetzner.delete_instance_errors = ["删除服务器 jp-1 失败"]  # the cleanup leaves the server
    assert deploy(hz) == 1
    assert hz.node().ssh.user == "root"
    del hz.server.fail_on["harden-stage.sh"]
    hz.hetzner.delete_instance_errors = []
    hz.hetzner.calls.clear()
    hz.terminal.inputs = ["已保存"]
    assert run_up(hz, hetzner="sg") == 0
    port = hz.node().ssh.port
    assert rules(hz) == [((22,), XRAY_PORTS), ((22, port), XRAY_PORTS), ((port,), XRAY_PORTS)]
