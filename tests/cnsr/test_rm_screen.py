"""What ``cnsr rm`` shows: the plan, then its stages; the steps' own lines go to the log."""

from __future__ import annotations

import pytest
from display_fakes import RecordingDisplay
from rm_harness import deploy_aws, deploy_vps, remove

from cnsr.commands import rm
from cnsr.state import Node


def test_rm_shows_its_stages(fake_deps):
    deploy_aws(fake_deps)
    code, _ = remove(fake_deps)
    assert code == 0
    screen = fake_deps.terminal.screen
    assert "cnsr rm jp-1" in screen
    assert "[1/4] 删除 AWS 实例和静态 IP" in screen and "[4/4] 删除控制端登记：完成" in screen
    assert "删除 AWS 上的静态 IP 和实例" not in screen and "删除 AWS 上的静态 IP 和实例" in fake_deps.terminal.log
    assert screen.rstrip().endswith("服务器 jp-1 已删除。")


def test_rm_of_a_vendor_node_has_three_stages(fake_deps):
    deploy_vps(fake_deps)
    code, _ = remove(fake_deps)
    assert code == 0
    screen = fake_deps.terminal.screen
    assert "[1/3] 删除 DNS 记录" in screen and "[3/3] 删除控制端登记：完成" in screen
    assert "AWS 实例" not in screen.split("cnsr rm jp-1", 1)[1]


def test_rm_stages_depend_on_the_source():
    assert rm.rm_stages(Node(alias="a", source="aws", ipv4="")) == ["aws", "dns", "alias", "record"]
    assert rm.rm_stages(Node(alias="b", source="vps", ipv4="")) == ["dns", "alias", "record"]


def test_the_display_begins_after_the_confirmation(fake_deps):
    deploy_aws(fake_deps)
    display = RecordingDisplay()
    fake_deps.deps.ui = fake_deps.terminal.ui(display=display)
    assert remove(fake_deps)[0] == 0
    names = [call[0] for call in display.calls]
    assert names.index("begin") > names.index("resumed")  # the confirmation question came first
    assert ("begin", "cnsr rm jp-1", list(rm.RM_TITLES.values())) in display.calls
    assert [call for call in display.calls if call[0] in ("start", "end")] == [
        ("start", 1), ("start", 2), ("start", 3), ("start", 4), ("end", "done")
    ]  # fmt: skip


def test_a_leftover_ends_the_display_as_failed(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.dns.deny("DELETE", "AAAA")
    display = RecordingDisplay()
    fake_deps.deps.ui = fake_deps.terminal.ui(display=display)
    assert remove(fake_deps)[0] == 1
    assert [call for call in display.calls if call[0] == "end"] == [("end", "failed")]
    lines = [call[1] for call in display.calls if call[0] == "line"]
    assert any("没有删干净" in line for line in lines) and any("AAAA" in line for line in lines)


@pytest.mark.parametrize(("deny", "failed_stage"), [("aws", 1), ("dns", 2)])
def test_the_part_that_left_something_is_the_stage_marked_failed(deny, failed_stage, fake_deps):
    deploy_aws(fake_deps)
    if deny == "aws":
        fake_deps.lightsail.delete_instance_errors = ["删除实例 jp-1 失败：AccessDenied"]
    else:
        fake_deps.dns.deny("DELETE", "AAAA")
    display = RecordingDisplay()
    fake_deps.deps.ui = fake_deps.terminal.ui(display=display)
    assert remove(fake_deps)[0] == 1
    expected = [("start", n, "failed") if n == failed_stage + 1 else ("start", n) for n in (1, 2, 3, 4)]
    assert [call for call in display.calls if call[0] in ("start", "end")] == [*expected, ("end", "failed")]


def test_rm_still_tries_every_part_after_one_leaves_something(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.delete_instance_errors = ["删除实例 jp-1 失败：AccessDenied"]
    assert remove(fake_deps)[0] == 1
    assert fake_deps.dns.records == {}
    assert "[1/4] 删除 AWS 实例和静态 IP：失败" in fake_deps.terminal.screen
    assert "[2/4] 删除 DNS 记录：完成" in fake_deps.terminal.screen


def test_an_interruption_ends_the_display_as_interrupted(fake_deps, monkeypatch):
    deploy_aws(fake_deps)
    display = RecordingDisplay()
    fake_deps.deps.ui = fake_deps.terminal.ui(display=display)

    def interrupted(cf, zone_id, node):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rm, "delete_node_records", interrupted)
    with pytest.raises(KeyboardInterrupt):
        remove(fake_deps)
    assert [call for call in display.calls if call[0] == "end"] == [("end", "interrupted")]
