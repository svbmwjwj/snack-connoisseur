"""What ``cnsr up`` puts on the screen (stages, questions, results) and what it leaves to the log."""

from __future__ import annotations

import pytest
from deploy_fakes import AWS_IP, VPS_IP, preflight_output, run_up
from display_fakes import RecordingDisplay

from cnsr.deploy.context import AddRequest
from cnsr.deploy.steps import heading
from cnsr.errors import CnsrError
from cnsr.scan.ranges import scan_targets

VPS_ANSWERS = ["", "", "已保存"]
REFUSED = preflight_output(listen=("0.0.0.0:22", "0.0.0.0:443"), user="admin")
CLEANUP_START = "部署没有完成，删除这次在 AWS 和 Cloudflare 上建的实例、静态 IP、DNS 记录和服务器登记……"
CLEANED = "已清理干净，不会继续计费。修好问题后重跑同一条命令会重新开一台。"


def recording(fakes) -> RecordingDisplay:
    display = RecordingDisplay()
    fakes.deps.ui = fakes.terminal.ui(display=display)
    return display


def test_the_heading_names_the_source():
    aws = AddRequest("jp-1", "aws", "jp-tokyo", None, "admin", 22)
    vps = AddRequest("box", "vps", None, VPS_IP, "root", 22)
    assert heading(aws) == "cnsr up jp-1 · AWS 日本东京"
    assert heading(AddRequest("us-1", "aws", "us-oregon", None, "admin", 22)) == "cnsr up us-1 · AWS 美国俄勒冈州"
    assert heading(vps) == f"cnsr up box · 已有服务器 {VPS_IP}"


def test_a_deployment_shows_stages_not_details(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    screen = fake_deps.terminal.screen
    assert "cnsr up jp-1 · AWS 日本东京" in screen
    assert "[1/8] 确定服务器地址" in screen and "[7/8] 登记节点并切换 SSH 别名：完成" in screen
    assert "安装 Docker" not in screen and "节点验证了" not in screen
    assert "节点验证了" in fake_deps.terminal.log


def test_preparing_says_what_it_is_doing(fake_deps):
    display = recording(fake_deps)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    busy = display.calls.index(("busy", "检查 Cloudflare 控制端"))
    assert display.calls[busy + 1] == ("idle",)
    assert busy < next(i for i, call in enumerate(display.calls) if call[0] == "begin")
    assert "检查 Cloudflare 控制端" in fake_deps.terminal.log


def test_a_control_plane_check_that_fails_clears_the_busy_line(fake_deps):
    display = recording(fake_deps)
    fake_deps.cloudflare.deny("GET", "/accounts/acct-1/storage/kv/namespaces", 500)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    busy = display.calls.index(("busy", "检查 Cloudflare 控制端"))
    assert display.calls[busy + 1] == ("idle",)


def test_the_scan_reports_its_count(fake_deps):
    display = recording(fake_deps)
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    counts = [call for call in display.calls if call[0] == "count"]
    total = len(scan_targets(AWS_IP))
    assert total == 3070 and counts[-1] == ("count", total, total)


def test_the_scan_details_go_to_the_log(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    log, screen = fake_deps.terminal.log, fake_deps.terminal.screen
    assert "198.51.96.0/20" in log and "203.0.113.5" in log  # the range and the direct-check addresses
    assert "site0.example.com（203.0.113.10）" in log  # a candidate sent to the node, with its IP
    assert "选定 SNI：site0.example.com" in log and "选定 SNI：site0.example.com" not in screen


def test_the_root_password_login_hands_the_terminal_over(fake_deps):
    display = recording(fake_deps)
    fake_deps.server.key_installed = False
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    paused = []
    install = fake_deps.deps.install_key

    def watched(*args):
        paused.append(display.paused_now)
        install(*args)

    fake_deps.deps.install_key = watched
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert paused == [True]
    (notice,) = [i for i, call in enumerate(display.calls) if call[0] == "line" and "root 密码" in call[1]]
    marks = [call for call in display.calls[:notice] if call in (("paused",), ("resumed",))]
    assert marks[-1] == ("paused",)  # below the stage's context line, not above the live block


def test_the_console_password_is_shown_transiently_and_kept_out_of_the_log(fake_deps):
    display = recording(fake_deps)
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    (fed,) = fake_deps.server.chpasswd
    password = fed.removeprefix("admin:").strip()
    assert len(password) == 20
    assert any(call[0] == "transient" and password in call[1] for call in display.calls)
    assert not any(call[0] == "line" and password in call[1] for call in display.calls)
    assert password not in fake_deps.terminal.log


def test_hardening_progress_is_only_logged(fake_deps):
    fake_deps.terminal.inputs = list(VPS_ANSWERS)
    assert run_up(fake_deps, ip=VPS_IP) == 0
    assert "开始加固" in fake_deps.terminal.log and "开始加固" not in fake_deps.terminal.screen


@pytest.mark.parametrize(("error", "outcome"), [(CnsrError("握手验证失败"), "failed"), (KeyboardInterrupt(), "interrupted")])
def test_a_failed_step_ends_the_display_as_failed(fake_deps, error, outcome):
    display = recording(fake_deps)
    fake_deps.server.fail_on["verify-reality.sh"] = error
    if outcome == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            run_up(fake_deps, aws="jp-tokyo")
    else:
        assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert [call for call in display.calls if call[0] == "end"] == [("end", outcome)]


def test_cleanup_is_one_busy_line_and_the_result(fake_deps):
    fake_deps.server.preflight = REFUSED
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    lines = fake_deps.terminal.out.getvalue().splitlines()
    failed = next(i for i, line in enumerate(lines) if line.startswith("[2/8] 连接服务器并建解析：失败"))
    assert lines[failed + 1 :] == [CLEANUP_START, CLEANED]
    assert CLEANUP_START.removesuffix("……") in fake_deps.terminal.log
    assert "删除 AWS 上的静态 IP 和实例" in fake_deps.terminal.log
    assert "删除 AWS 上的静态 IP 和实例" not in fake_deps.terminal.screen


def test_typed_aws_keys_stay_out_of_the_log(fake_deps):
    fake_deps.aws_chain = False
    key_id, secret = "AKIAEXAMPLE0001", "wJalrXUtnFEMI-example-secret"
    fake_deps.terminal.inputs = [key_id, secret]
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert fake_deps.calls["make_lightsail"][0] == ("jp-tokyo", (key_id, secret))
    assert key_id not in fake_deps.terminal.log and secret not in fake_deps.terminal.log
