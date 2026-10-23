"""``cnsr add`` step 8: the node itself reaches the control plane before the add is done."""

from __future__ import annotations

from deploy_fakes import run_add

from cnsr.deploy import steps_rotate

CHECK = "/opt/cnsr/bin/cnsr-rotate.sh check"
NODE_HOST = "cnsr-control.acct-sub.workers.dev"


def test_add_ends_with_the_node_reaching_the_control_plane(fake_deps, caplog):
    assert run_add(fake_deps, aws="jp") == 0
    what = fake_deps.server.what()
    assert what.index("systemctl enable --now cnsr-rotate.service") < what.index(CHECK)
    assert fake_deps.server.control_checks == 1
    assert f"让节点自己连一次控制端 {NODE_HOST}" in caplog.text and "节点能连上控制端" in caplog.text


def test_a_node_record_or_address_still_on_its_way_is_waited_for(fake_deps):
    # A new node's record, or a workers.dev address enabled moments ago, can take a few minutes
    # to reach the edge the node talks to.
    assert steps_rotate.CHECK_SECONDS >= 300
    fake_deps.server.control_answers = ["401", "000", "404", "200"]
    assert run_add(fake_deps, aws="jp") == 0
    assert fake_deps.server.control_checks == 4
    assert fake_deps.sleeps.count(steps_rotate.CHECK_INTERVAL_SECONDS) == 3


def test_a_cloudflare_challenge_stops_the_add_at_once(fake_deps):
    fake_deps.server.control_answers = ["403"]
    assert run_add(fake_deps, aws="jp") == 1
    screen = fake_deps.terminal.screen
    assert fake_deps.server.control_checks == 1
    assert "[8/8] 启动换 SNI 服务：失败" in screen
    assert f"节点连控制端 {NODE_HOST} 被 Cloudflare 拦下了（HTTP 403）" in screen
    assert "重跑同一条命令" in screen


def test_a_control_plane_the_node_never_reaches_stops_the_add(fake_deps):
    fake_deps.server.control_answers = ["000"]
    assert run_add(fake_deps, aws="jp") == 1
    screen = fake_deps.terminal.screen
    assert fake_deps.server.control_checks == steps_rotate.CHECK_SECONDS // steps_rotate.CHECK_INTERVAL_SECONDS + 1
    assert f"节点连不上控制端 {NODE_HOST}（没有收到回应）" in screen


def test_a_token_the_control_plane_keeps_refusing_stops_the_add(fake_deps):
    fake_deps.server.control_answers = ["401"]
    assert run_add(fake_deps, aws="jp") == 1
    assert f"节点连不上控制端 {NODE_HOST}（控制端不认节点 token，HTTP 401）" in fake_deps.terminal.screen


def test_a_service_that_waited_out_refused_tokens_is_restarted_once_the_node_gets_through(fake_deps):
    # The service gives up after 401s for 2 minutes; the check may get through only after that.
    fake_deps.server.control_answers = ["401", "401", "200"]
    assert run_add(fake_deps, aws="jp") == 0
    what = fake_deps.server.what()
    check = len(what) - 1 - what[::-1].index(CHECK)
    after = what[check:]
    assert "systemctl restart cnsr-rotate.service" in after
    assert any(step.startswith("rm -f /opt/cnsr/stopped") for step in after)
    assert fake_deps.server.service == "active"


def test_a_node_that_gets_through_at_once_keeps_its_service_running(fake_deps):
    assert run_add(fake_deps, aws="jp") == 0
    what = fake_deps.server.what()
    assert "systemctl restart cnsr-rotate.service" not in what[what.index(CHECK):]
