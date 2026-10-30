"""An SSH port this computer cannot reach: the control plane tries it from outside to tell why."""

from __future__ import annotations

from deploy_fakes import run_up
from lightsail_fakes import AWS_IP

from cnsr.cloudflare.control import ProbeResult
from cnsr.scan.egress import Egress
from cnsr.ssh.remote import SshError, classify


def unreachable(fake_deps, outside: ProbeResult) -> None:
    """ssh times out from here; this computer's network drops the SSH port."""
    fake_deps.server.fail_on["true"] = SshError("port")
    fake_deps.server.dropped_ports = {22}
    fake_deps.probe_results[f"{AWS_IP}:22"] = [outside] * 100  # every try in the boot wait


def test_a_port_open_from_outside_points_at_this_computers_network(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=True, ms=180, error=None))
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    text = fake_deps.terminal.text
    assert f"从外部能连上 {AWS_IP} 的 22 端口，这台电脑却连不上" in text
    assert "当前网络" in text and "代理" in text
    assert f"{AWS_IP}:22" in fake_deps.calls["probe"]


def test_a_port_closed_from_outside_too_points_at_the_server(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=False, ms=None, error="连接超时"))
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert f"从外部也连不上 {AWS_IP} 的 22 端口（连接超时）" in fake_deps.terminal.text


def test_without_the_control_plane_the_plain_reason_stays(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=False, ms=None, error="无法连接控制端", control_reached=False))
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    text = fake_deps.terminal.text
    assert "连不上 SSH 端口" in text and "从外部能连上" not in text and "从外部也连不上" not in text


def test_a_known_node_is_tried_on_its_own_ssh_port(fake_deps):
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    port = fake_deps.node().ssh.port
    fake_deps.server.fail_on["true"] = SshError("port")
    fake_deps.server.dropped_ports = {port}
    fake_deps.probe_results[f"{fake_deps.node().domain}:{port}"] = [ProbeResult(ok=True, ms=90, error=None)]
    fake_deps.probe_results[f"{AWS_IP}:{port}"] = [ProbeResult(ok=True, ms=90, error=None)]
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert f"的 {port} 端口，这台电脑却连不上" in fake_deps.terminal.text


def test_an_unreachable_network_is_a_port_problem():
    assert classify("ssh: connect to host 198.51.100.248 port 22: Network is unreachable") == "port"


def test_a_proxy_left_on_is_named(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=True, ms=180, error=None))
    fake_deps.egress_result = Egress("203.0.113.5", "203.0.113.5", fake_dns=True)
    fake_deps._seen = fake_deps.egress_result
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    text = fake_deps.terminal.text
    assert "代理软件的虚拟地址" in text and "关掉代理" in text


def test_an_sshd_that_just_came_up_is_logged_in_to_again(fake_deps):
    # ssh timed out while sshd was starting; by the outside probe it answers, here too.
    fake_deps.server.fail_on["true"] = SshError("port")
    calls = []

    def sshd_up_now(meta, host, port=443):
        calls.append(port)
        fake_deps.server.fail_on.pop("true", None)
        return ProbeResult(ok=True, ms=50, error=None)

    fake_deps.deps.probe = sshd_up_now
    assert run_up(fake_deps, aws="jp-tokyo") == 0
    assert 22 in calls


def test_a_refused_connection_is_not_diagnosed(fake_deps):
    fake_deps.server.fail_on["true"] = SshError("port", unreachable=False)
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "22" not in " ".join(fake_deps.calls["probe"])


def test_only_timeouts_count_as_unreachable():
    from cnsr.ssh.remote import _connection_error

    assert _connection_error("ssh: connect to host h port 22: Operation timed out").unreachable
    assert _connection_error("ssh: connect to host h port 22: Network is unreachable").unreachable
    assert not _connection_error("ssh: connect to host h port 22: Connection refused").unreachable
    assert not _connection_error("Connection timed out during banner exchange").unreachable


def test_a_dropped_port_on_a_working_address_is_the_path(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=True, ms=180, error=None))
    fake_deps.server.dropped_ports = {22, 80}  # 443 still refuses: the address answers
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "这台电脑到服务器的路上" in fake_deps.terminal.text and "被墙" not in fake_deps.terminal.text


def test_no_route_to_the_ssh_port_is_not_taken_for_an_answer(fake_deps):
    unreachable(fake_deps, ProbeResult(ok=True, ms=180, error=None))
    fake_deps.server.erroring_ports = {22}  # "No route to host" here, open from outside
    assert run_up(fake_deps, aws="jp-tokyo") == 1
    assert "这台电脑到服务器的路上" in fake_deps.terminal.text


def test_the_outside_check_is_announced_once_per_address(fake_deps, caplog):
    fake_deps.server.fail_on["true"] = SshError("port")  # sshd down for everyone: keep waiting
    run_up(fake_deps, aws="jp-tokyo")
    assert caplog.text.count("请控制端从外部试一次") == 1
