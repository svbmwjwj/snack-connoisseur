"""``cnsr rm`` on AWS: which instance and static IP may be deleted, and what a rerun finishes.

The static IPs cannot be tagged, so a name alone proves nothing: an IP is released only when
the record names it and it is free or attached to the node's instance, or when it is
``<alias>-ip`` attached to the node's instance.
"""

from __future__ import annotations

from deploy_fakes import run_add
from rm_harness import deploy_aws, entry_deleted, remove

from cnsr import localconf
from cnsr.ssh.config import read_block

TAG_PROBLEM = "实例 jp-1 不是 cnsr 创建的，未删除"
# Without a recorded instance, rm looks for one named after the alias that carries cnsr's tag.
LOOK_FOR_INSTANCE = ("instance_tagged", "jp-1", "jp-1")
ALL_CALLS = [
    ("instance_tagged", "jp-1", "jp-1"),
    ("static_ip_attachment", "jp-1-ip"),
    ("release_static_ip", "jp-1-ip"),
    ("delete_instance", "jp-1"),
]


def forget_static_ip_name(fakes) -> None:
    """The record as a crash between attaching the IP and saving its name leaves it."""
    node = fakes.node()
    node.static_ip_name = None
    fakes.store(node)


def lose_instance(fakes) -> None:
    """The user deleted the instance by hand: its static IP is free again."""
    fakes.lightsail.delete_instance("jp-1")
    fakes.lightsail.calls.clear()


def names_in_record(fakes) -> tuple[str | None, str | None]:
    node = fakes.node()
    return node.instance_name, node.static_ip_name


# --- which names are used


def test_the_record_decides_which_names_are_deleted(fake_deps):
    deploy_aws(fake_deps)
    node = fake_deps.node()
    node.instance_name, node.static_ip_name = "jp-1-old", "jp-1-old-ip"
    fake_deps.store(node)
    fake_deps.lightsail.instances = {"jp-1-old": True}
    fake_deps.lightsail.ips = {"jp-1-old-ip": "jp-1-old"}
    assert remove(fake_deps)[0] == 0
    assert fake_deps.lightsail.calls == [
        ("instance_tagged", "jp-1-old", "jp-1"),
        ("static_ip_attachment", "jp-1-old-ip"),
        ("release_static_ip", "jp-1-old-ip"),
        ("delete_instance", "jp-1-old"),
    ]


def forget_instance_name(fakes) -> None:
    """The record as a crash between creating the instance and saving its name leaves it."""
    node = fakes.node()
    node.instance_name = node.static_ip_name = None
    fakes.store(node)


def test_without_recorded_names_only_an_instance_named_after_the_alias_is_looked_for(fake_deps):
    deploy_aws(fake_deps)
    forget_instance_name(fake_deps)
    lose_instance(fake_deps)
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE]
    (listed,) = fake_deps.terminal.seen
    assert "实例 jp-1" in listed and "记录里没有它" in listed
    assert fake_deps.lightsail.ips == {"jp-1-ip": None}  # a free IP is never guessed without cnsr's instance
    assert fake_deps.node() is None


def test_an_unrecorded_instance_with_cnsrs_tag_is_cnsrs_and_deleted_with_its_ip(fake_deps):
    deploy_aws(fake_deps)
    forget_instance_name(fake_deps)
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [
        ("instance_tagged", "jp-1", "jp-1"),
        ("static_ip_attachment", "jp-1-ip"),
        ("release_static_ip", "jp-1-ip"),
        ("delete_instance", "jp-1"),
    ]
    assert fake_deps.lightsail.instances == {} and fake_deps.lightsail.ips == {}
    assert fake_deps.node() is None


def test_an_unrecorded_instance_without_cnsrs_tag_is_left_alone(fake_deps):
    deploy_aws(fake_deps)
    forget_instance_name(fake_deps)
    fake_deps.lightsail.instances["jp-1"] = False  # the user's own instance of that name
    code, out = remove(fake_deps)
    assert code == 0 and "未删除" not in out
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE]
    assert fake_deps.lightsail.instances == {"jp-1": False} and fake_deps.lightsail.ips == {"jp-1-ip": "jp-1"}


def test_an_unrecorded_instance_that_cannot_be_looked_for_is_a_warning(fake_deps):
    deploy_aws(fake_deps)
    forget_instance_name(fake_deps)
    fake_deps.aws_chain = False
    code, out = remove(fake_deps)
    assert code == 0
    assert "警告：" in out and "Lightsail 控制台" in out
    assert fake_deps.node() is None


# --- the instance


def test_an_instance_cnsr_did_not_create_is_never_deleted_nor_is_its_ip(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.instances["jp-1"] = False

    code, out = remove(fake_deps)

    assert code == 1
    assert TAG_PROBLEM in out
    assert not entry_deleted(fake_deps)
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE]
    assert fake_deps.lightsail.ips == {"jp-1-ip": "jp-1"}
    assert names_in_record(fake_deps) == ("jp-1", "jp-1-ip")
    assert fake_deps.dns.records == {}  # the other steps still run

    del fake_deps.lightsail.instances["jp-1"]  # the user deleted it by hand
    fake_deps.lightsail.ips["jp-1-ip"] = None
    code, _ = remove(fake_deps)
    assert code == 0
    assert fake_deps.node() is None


def test_a_missing_instance_is_skipped_but_its_recorded_static_ip_is_released(fake_deps):
    deploy_aws(fake_deps)
    lose_instance(fake_deps)
    code, out = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [
        ("instance_tagged", "jp-1", "jp-1"),
        ("static_ip_attachment", "jp-1-ip"),
        ("release_static_ip", "jp-1-ip"),
    ]
    assert fake_deps.lightsail.ips == {} and fake_deps.node() is None


def test_an_instance_that_cannot_be_deleted_keeps_the_record_and_a_rerun_retries_only_it(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.delete_instance_errors = ["Lightsail 操作失败（删除实例，delete_instance，ap-northeast-1）：没有权限"]

    code, out = remove(fake_deps)

    assert code == 1
    assert "没有权限" in out and "没有删干净" in out
    assert names_in_record(fake_deps) == ("jp-1", None)  # the IP is done
    assert fake_deps.dns.records == {}
    assert read_block(fake_deps.deps.ssh_config, "jp-1") is None

    fake_deps.lightsail.delete_instance_errors = []
    fake_deps.lightsail.calls.clear()
    code, _ = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [
        ("instance_tagged", "jp-1", "jp-1"),
        ("static_ip_attachment", "jp-1-ip"),  # the candidate name again: nothing there, nothing released
        ("delete_instance", "jp-1"),
    ]
    assert fake_deps.node() is None


# --- the recorded static IP


def test_a_recorded_static_ip_attached_to_the_instance_is_released(fake_deps):
    deploy_aws(fake_deps)
    assert fake_deps.lightsail.ips == {"jp-1-ip": "jp-1"}
    assert remove(fake_deps)[0] == 0
    assert fake_deps.lightsail.calls == ALL_CALLS


def test_a_recorded_static_ip_that_is_free_is_released(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.ips["jp-1-ip"] = None
    assert remove(fake_deps)[0] == 0
    assert fake_deps.lightsail.calls == ALL_CALLS
    assert fake_deps.lightsail.ips == {}


def test_a_recorded_static_ip_that_is_gone_needs_nothing(fake_deps):
    deploy_aws(fake_deps)
    del fake_deps.lightsail.ips["jp-1-ip"]
    assert remove(fake_deps)[0] == 0
    assert [call[0] for call in fake_deps.lightsail.calls] == ["instance_tagged", "static_ip_attachment", "delete_instance"]
    assert fake_deps.node() is None


def test_a_recorded_static_ip_attached_to_another_instance_is_left_alone(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.ips["jp-1-ip"] = "web"

    code, out = remove(fake_deps)

    assert code == 1
    assert "静态 IP jp-1-ip 绑在实例 web 上，未释放" in out
    assert "release_static_ip" not in fake_deps.lightsail.names()
    assert fake_deps.lightsail.ips == {"jp-1-ip": "web"}
    assert names_in_record(fake_deps) == (None, "jp-1-ip")  # the instance is gone, the IP is still to check
    assert not entry_deleted(fake_deps)

    fake_deps.lightsail.calls.clear()
    code, out = remove(fake_deps)  # nothing changed: the same answer, the same care
    assert code == 1 and "绑在实例 web 上" in out
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE, ("static_ip_attachment", "jp-1-ip")]

    fake_deps.lightsail.ips["jp-1-ip"] = None  # the user moved it away
    fake_deps.lightsail.calls.clear()
    code, _ = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE, ("static_ip_attachment", "jp-1-ip"), ("release_static_ip", "jp-1-ip")]
    assert fake_deps.node() is None


def test_a_static_ip_that_cannot_be_released_keeps_the_record_and_a_rerun_retries_only_it(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.lightsail.release_errors = ["Lightsail 操作失败（释放静态 IP，release_static_ip，ap-northeast-1）：没有权限"]

    code, out = remove(fake_deps)

    assert code == 1
    assert "释放静态 IP" in out
    assert names_in_record(fake_deps) == (None, "jp-1-ip")  # the instance is gone
    assert fake_deps.lightsail.instances == {}

    fake_deps.lightsail.release_errors = []
    fake_deps.lightsail.calls.clear()
    code, _ = remove(fake_deps)
    assert code == 0
    assert fake_deps.lightsail.calls == [LOOK_FOR_INSTANCE, ("static_ip_attachment", "jp-1-ip"), ("release_static_ip", "jp-1-ip")]
    assert fake_deps.node() is None


# --- no recorded static IP: <alias>-ip is only a candidate


def test_an_unrecorded_static_ip_attached_to_the_instance_is_cnsrs_and_released(fake_deps):
    deploy_aws(fake_deps)
    forget_static_ip_name(fake_deps)
    assert remove(fake_deps)[0] == 0
    assert "只有它绑在实例 jp-1 上才会释放" in fake_deps.terminal.seen[0]
    assert fake_deps.lightsail.calls == ALL_CALLS
    assert fake_deps.lightsail.ips == {} and fake_deps.node() is None


def test_an_unrecorded_free_static_ip_is_left_for_the_user_to_check(fake_deps):
    deploy_aws(fake_deps)
    forget_static_ip_name(fake_deps)
    fake_deps.lightsail.ips["jp-1-ip"] = None

    code, out = remove(fake_deps)

    assert code == 0
    assert "静态 IP jp-1-ip 没有绑在任何实例上，可能是 cnsr 留下的" in out and "Lightsail 控制台" in out
    assert "release_static_ip" not in fake_deps.lightsail.names()
    assert fake_deps.lightsail.ips == {"jp-1-ip": None}
    assert fake_deps.lightsail.instances == {}
    assert fake_deps.node() is None


def test_an_unrecorded_static_ip_attached_elsewhere_is_ignored(fake_deps):
    deploy_aws(fake_deps)
    forget_static_ip_name(fake_deps)
    fake_deps.lightsail.ips["jp-1-ip"] = "web"

    code, out = remove(fake_deps)

    assert code == 0
    assert "可能是 cnsr 留下的" not in out and "未释放" not in out  # no warning, no failure
    assert fake_deps.lightsail.ips == {"jp-1-ip": "web"}
    assert "release_static_ip" not in fake_deps.lightsail.names()
    assert fake_deps.lightsail.instances == {}


def test_an_unrecorded_static_ip_that_does_not_exist_needs_nothing(fake_deps):
    deploy_aws(fake_deps)
    forget_static_ip_name(fake_deps)
    del fake_deps.lightsail.ips["jp-1-ip"]
    assert remove(fake_deps)[0] == 0
    assert "release_static_ip" not in fake_deps.lightsail.names()


def test_an_unrecorded_static_ip_proven_cnsrs_is_recorded_before_it_is_released(fake_deps):
    deploy_aws(fake_deps)
    forget_static_ip_name(fake_deps)
    fake_deps.lightsail.release_errors = ["Lightsail 操作失败（释放静态 IP，release_static_ip，ap-northeast-1）：没有权限"]

    code, out = remove(fake_deps)

    assert code == 1 and "没有权限" in out
    assert names_in_record(fake_deps) == (None, "jp-1-ip")  # a rerun knows the IP is cnsr's

    fake_deps.lightsail.release_errors = []
    fake_deps.lightsail.calls.clear()
    code, _ = remove(fake_deps)
    assert code == 0
    assert ("release_static_ip", "jp-1-ip") in fake_deps.lightsail.calls
    assert fake_deps.lightsail.ips == {} and fake_deps.node() is None


# --- a strangers static IP: the scenario that started it


def test_add_that_stops_at_a_strangers_static_ip_leaves_it_alone_for_rm(fake_deps):
    fake_deps.lightsail.ips["jp-1-ip"] = "web"  # the user's own, attached to their instance "web"
    fake_deps.lightsail.delete_instance_errors = ["删除实例 jp-1 失败"]  # add's own cleanup leaves it to rm

    assert run_add(fake_deps, aws="jp") == 1
    assert "绑在实例 web 上" in fake_deps.terminal.text
    assert names_in_record(fake_deps) == ("jp-1", None)  # the name is recorded only once the IP is cnsr's

    fake_deps.lightsail.delete_instance_errors = []
    code, out = remove(fake_deps)

    assert code == 0
    assert fake_deps.lightsail.instances == {}  # cnsr's own instance is deleted
    assert fake_deps.lightsail.ips == {"jp-1-ip": "web"}  # the user's IP is not
    assert "release_static_ip" not in fake_deps.lightsail.names()
    assert fake_deps.node() is None


# --- credentials


def test_missing_aws_credentials_keep_the_record(fake_deps):
    deploy_aws(fake_deps)
    fake_deps.aws_chain = False

    code, out = remove(fake_deps)

    assert code == 1
    assert "AWS 凭据" in out
    assert fake_deps.calls["make_lightsail"] == [] and fake_deps.lightsail.calls == []
    assert fake_deps.node() is not None
    assert fake_deps.dns.records == {}

    values = localconf.load(fake_deps.deps.conf_path)
    localconf.save(fake_deps.deps.conf_path, {**values, "AWS_ACCESS_KEY_ID": "AKIDSAVED", "AWS_SECRET_ACCESS_KEY": "s"})
    code, _ = remove(fake_deps)
    assert code == 0
    assert fake_deps.calls["make_lightsail"] == [("jp", ("AKIDSAVED", "s"))]
    assert fake_deps.node() is None
