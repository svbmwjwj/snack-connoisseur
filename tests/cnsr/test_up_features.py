"""Tests for choosing what ``cnsr up`` deploys: the base hardening alone, or with the Xray node."""

from __future__ import annotations

import re

import pytest
from deploy_fakes import VPS_IP, run_up

from cnsr import features
from cnsr.errors import CnsrError
from cnsr.server.harden import DROP_IN
from cnsr.server.xray import CONFIG_PATH, PORTS

HARDEN_ONLY = [features.HARDEN]
BOTH = list(features.ALL)
VPS_LOGIN = ["", ""]  # SSH user and port: the defaults
SAVED = "已保存"  # the console password is stored


def stage_totals(fake_deps) -> set[str]:
    return set(re.findall(r"^\[\d/(\d)\] ", fake_deps.terminal.screen, flags=re.M))


# --- the menu --------------------------------------------------------------------------------


def test_a_new_server_asks_and_takes_both_on_enter(fake_deps):
    fake_deps.terminal.inputs = [""]
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert "要部署哪些功能？" in fake_deps.terminal.out.getvalue()
    assert fake_deps.node().features == BOTH


def test_ticking_only_the_base_deploys_only_the_hardening(fake_deps):
    fake_deps.terminal.inputs = ["1"]
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert fake_deps.node().features == HARDEN_ONLY


def test_an_extension_ticked_alone_brings_the_base_with_it(fake_deps):
    fake_deps.terminal.inputs = ["2"]
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert "必须同时选择基础加固" not in fake_deps.terminal.out.getvalue()
    assert fake_deps.node().features == BOTH


def test_a_server_with_every_feature_is_not_asked_again(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0  # no answer queued: a question would fail
    assert fake_deps.node().features == BOTH


def test_a_record_from_before_features_counts_as_both(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    old = fake_deps.node()
    old.features = []
    fake_deps.store(old)
    assert features.installed(old) == BOTH
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0


def test_the_hardening_of_a_hardened_server_cannot_be_unticked(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=HARDEN_ONLY) == 0
    fake_deps.terminal.inputs = ["2"]  # only Xray typed: the locked base stays
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert "1. 基础加固（已部署，不能取消）" in fake_deps.terminal.out.getvalue()
    assert fake_deps.node().features == BOTH


def test_without_a_terminal_every_feature_is_deployed(fake_deps):
    fake_deps.deps.ui = fake_deps.terminal.ui(interactive=False)
    assert run_up(fake_deps, aws="jp-tokyo", features=None) == 0
    assert fake_deps.node().features == BOTH


@pytest.mark.parametrize("chosen", [[], [features.XRAY]])
def test_a_choice_without_the_base_stops_before_anything_is_created(fake_deps, chosen):
    assert run_up(fake_deps, aws="jp-tokyo", features=chosen) == 1
    assert fake_deps.lightsail.names() == []


# --- what the hardening alone does ------------------------------------------------------------


def test_harden_only_on_aws_runs_three_steps_and_opens_only_ssh(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=HARDEN_ONLY) == 0
    assert stage_totals(fake_deps) == {"3"}
    assert fake_deps.lightsail.app_ports == ()
    assert "Lightsail 防火墙只放行 22。" in fake_deps.terminal.text
    assert not fake_deps.calls["scan"] and not fake_deps.server.did("install-docker.sh")
    node = fake_deps.node()
    assert node.features == HARDEN_ONLY and node.xray is None and node.line is None
    assert "已加固" in fake_deps.terminal.out.getvalue() and "圈X" not in fake_deps.terminal.out.getvalue()


def test_harden_only_on_a_vendor_server_opens_no_app_port(fake_deps):
    fake_deps.terminal.inputs = [*VPS_LOGIN, SAVED]
    assert run_up(fake_deps, ip=VPS_IP, features=HARDEN_ONLY) == 0
    assert fake_deps.server.script_args["harden-stage.sh"][3] == ""
    assert fake_deps.node().features == HARDEN_ONLY


def test_the_full_deployment_opens_the_xray_ports_on_a_vendor_server(fake_deps):
    fake_deps.terminal.inputs = [*VPS_LOGIN, SAVED]
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert fake_deps.server.script_args["harden-stage.sh"][3] == "443/tcp 443/udp"


def test_the_full_deployment_opens_the_xray_ports_on_aws(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.lightsail.app_ports == PORTS


# --- adding the Xray node later ---------------------------------------------------------------


def test_xray_added_to_a_hardened_vendor_server_opens_its_ports_and_deploys(fake_deps):
    fake_deps.terminal.inputs = [*VPS_LOGIN, SAVED]
    assert run_up(fake_deps, ip=VPS_IP, features=HARDEN_ONLY) == 0
    assert fake_deps.server.ufw_allowed == set()
    fake_deps.server.files[DROP_IN] = f"Port {fake_deps.node().ssh.port}\n"  # what harden-stage.sh left
    assert run_up(fake_deps) == 0
    assert fake_deps.server.ufw_allowed == {"443/tcp", "443/udp"}
    assert CONFIG_PATH in fake_deps.server.files
    node = fake_deps.node()
    assert node.features == BOTH and node.line is not None


def test_a_failed_xray_install_keeps_a_hardened_aws_server(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo", features=HARDEN_ONLY) == 0
    fake_deps.server.fail_on["verify-reality.sh"] = CnsrError("握手验证失败")
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    # It was in use before this run: nothing is deleted, and it stays recorded as hardened.
    assert "delete_instance" not in fake_deps.lightsail.names()
    assert fake_deps.node().features == HARDEN_ONLY


# --- the feature rules ------------------------------------------------------------------------


def test_the_rules_of_a_choice():
    assert features.problem([features.HARDEN]) is None
    assert features.problem(BOTH) is None
    assert "至少选择一项" in features.problem([])
    assert "必须同时选择基础加固" in features.problem([features.XRAY])
    assert features.app_ports(HARDEN_ONLY) == ()
    assert features.app_ports(BOTH) == PORTS
    assert features.summary(HARDEN_ONLY) == "仅加固" and features.summary(BOTH) == "Xray 节点"
