"""``cnsr up --hetzner``: the source and region menus, the token, the size tier and reruns.

These run ``prepare`` alone: what the pipeline does with a Hetzner server comes in later steps.
"""

from __future__ import annotations

import pytest
from deploy_fakes import up_args
from hetzner_fakes import offer

from cnsr import localconf
from cnsr.cloudflare.control import ensure_control_plane
from cnsr.commands import up
from cnsr.errors import CnsrError
from cnsr.sizes import describe, pick
from cnsr.ssh.config import HostBlock, write_block
from cnsr.state import Node, SshInfo

PREFIX = "等价命令（换一个别名即可按同样的配置再开一台）："
ENV_TOKEN = "hz-env-token-0001"


@pytest.fixture
def hz(fake_deps):
    """The fakes with a Hetzner token in ``HCLOUD_TOKEN``."""
    fake_deps.hcloud_env = ENV_TOKEN
    return fake_deps


def prepare(fakes, alias: str | None = "jp-1", **options):
    return up.prepare(up_args(alias, **options), fakes.deps)


def equivalent(fakes) -> list[str]:
    lines = fakes.terminal.screen.splitlines()
    return [line.removeprefix(PREFIX) for line in lines if line.startswith(PREFIX)]


def store(fakes, **fields) -> Node:
    """Store jp-1's record, Hetzner sg 1c1g unless ``fields`` say otherwise, in a new control plane."""
    ensure_control_plane(fakes.deps.make_cf("t-secret"), "example.com", fakes.deps.rng)
    node = Node(**{"alias": "jp-1", "source": "hetzner", "ipv4": "", "region": "sg", "size": "1c1g", **fields})
    fakes.store(node)
    return node


# --- the menus -------------------------------------------------------------------------------


def test_the_source_menu_offers_hetzner(hz):
    hz.terminal.inputs = ["3", "", "4", ""]  # source: 3 = Hetzner, plan: default, region: 4 = de-falkenstein, type: first
    ctx = prepare(hz)
    assert "3. 在 Hetzner Cloud 新开一台" in hz.terminal.screen
    assert (ctx.req.source, ctx.req.region, ctx.req.size) == ("hetzner", "de-falkenstein", "cpx02")
    assert ctx.hetzner is hz.hetzner and ctx.offer.name == "cpx02"
    assert hz.calls["make_hetzner"][-1] == ("de-falkenstein", ENV_TOKEN)


def test_the_region_menu_shows_the_codes_with_their_names(hz):
    hz.terminal.inputs = ["3", "", "6", ""]  # source: Hetzner, plan: default, region: 6 = fi-helsinki, type: first
    ctx = prepare(hz)
    screen = hz.terminal.screen
    for line in ("1. sg（新加坡）", "2. us-oregon（美国俄勒冈州）", "5. de-nuremberg（德国纽伦堡）", "6. fi-helsinki（芬兰赫尔辛基）"):
        assert line in screen
    assert "3 种机型，4.76 欧元/月起" in screen.split("Hetzner 区域")[1]
    assert "jp-tokyo" not in screen.split("Hetzner 区域")[1]
    assert ctx.req.region == "fi-helsinki"


def test_hetzner_logs_in_as_root_on_port_22(hz):
    ctx = prepare(hz, hetzner="sg", size="1c1g")
    assert (ctx.req.user, ctx.req.port) == ("root", 22)
    assert (ctx.node.ssh.user, ctx.node.ssh.port) == ("root", 22)


def test_the_plan_menu_lists_the_three_plans_with_their_count_and_lowest_price(hz):
    hz.terminal.inputs = ["", ""]  # plan: the default, type: the first
    ctx = prepare(hz, hetzner="sg")
    screen = hz.terminal.screen
    plans = screen.split("Hetzner 方案\n")[1].split("Hetzner 机型")[0]
    assert "1. Cost-Optimized       共享 vCPU，较旧硬件，最便宜，常缺货    1 种机型，5.1 欧元/月起" in plans
    assert "2. Regular Performance  共享 vCPU，新硬件，性价比高" in plans and "3 种机型，4.76 欧元/月起" in plans
    assert "3. Dedicated            独享 vCPU，性能稳定，较贵" in plans
    assert "（直接回车：2）" in hz.terminal.prompts[0]  # the plan of the cheapest type
    assert (ctx.req.size, ctx.offer.name) == ("cpx02", "cpx02")


def test_the_types_of_the_plan_are_listed_cheapest_first(hz):
    hz.terminal.inputs = ["2", "2"]
    ctx = prepare(hz, hetzner="sg")
    types = hz.terminal.screen.split("Hetzner 机型（月价含税，含 IPv4 地址）\n")[1]
    assert "1. 1c1g  cpx02  1 核  1 GB 内存  40 GB 磁盘  含 20 TB 流量，超出 1.19 欧元/TB   4.76 欧元/月" in types
    assert "2. 2c4g  cpx22  2 核  4 GB 内存" in types and "3. 4c8g  cpx32" in types and "cx23" not in types
    assert "（0 返回）" in hz.terminal.prompts[1]
    assert (ctx.req.size, ctx.offer.name) == ("cpx22", "cpx22")


def test_a_cost_optimized_type_can_be_picked(hz):
    hz.terminal.inputs = ["1", ""]
    assert prepare(hz, hetzner="sg").offer.name == "cx23"


def test_a_plan_with_nothing_on_sale_is_listed_but_cannot_be_picked(hz):
    hz.hetzner.stock = [o for o in hz.hetzner.stock if o.plan != "cost_optimized"]
    hz.terminal.inputs = ["1", "3", ""]
    ctx = prepare(hz, hetzner="sg")
    assert "1. Cost-Optimized       共享 vCPU，较旧硬件，最便宜，常缺货（该区域暂无货）" in hz.terminal.screen
    assert "第 1 项该区域暂无货，不能选，请选别的。" in hz.terminal.screen
    assert ctx.offer.name == "ccx13"


def test_back_in_the_types_shows_the_plans_again(hz):
    hz.terminal.inputs = ["2", "0", "3", ""]  # regular, back, dedicated, ccx13
    ctx = prepare(hz, hetzner="sg")
    assert hz.terminal.screen.count("Hetzner 方案\n") == 2 and ctx.offer.name == "ccx13"


def test_nothing_to_buy_in_the_region_stops_before_the_keys(hz):
    hz.hetzner.stock = []
    with pytest.raises(CnsrError, match="Hetzner sg（新加坡）暂时没有能买的机型"):
        prepare(hz, hetzner="sg")
    assert hz.node() is None


# --- --size ----------------------------------------------------------------------------------


def test_size_names_the_type_and_shows_its_line_without_a_menu(hz):
    ctx = prepare(hz, hetzner="sg", size="cx23")  # no answer queued
    assert f"Hetzner 机型：{describe(hz.hetzner.pick('cx23'))}" in hz.terminal.out.getvalue()
    assert ("pick", "cx23") in hz.hetzner.calls and ("offers",) not in hz.hetzner.calls
    assert ctx.offer.name == "cx23" and ctx.node.size == "cx23"


def test_a_size_recorded_before_types_were_named_is_still_read(hz):
    assert prepare(hz, hetzner="sg", size="2c4g").offer.name == "cx23"  # the cheapest shared type that meets it


def test_a_type_not_on_sale_stops(hz):
    with pytest.raises(CnsrError, match="cx33"):
        prepare(hz, hetzner="sg", size="cx33")


def test_a_size_that_is_no_type_stops_before_anything(hz):
    with pytest.raises(CnsrError, match="不是 Hetzner 机型"):
        prepare(hz, hetzner="sg", size="big-one")
    assert hz.hetzner.calls == []


def test_without_a_terminal_the_size_is_required(hz):
    hz.deps.ui = hz.terminal.ui(interactive=False)
    with pytest.raises(CnsrError, match="缺少参数：Hetzner 机型"):
        prepare(hz, hetzner="sg")
    assert hz.hetzner.calls == []


def test_without_a_terminal_size_is_enough(hz):
    hz.deps.ui = hz.terminal.ui(interactive=False)
    assert prepare(hz, hetzner="sg", size="1c1g").offer.name == "cpx02"


@pytest.mark.parametrize("source", [{"ip": "198.51.100.7"}, {}])
def test_size_needs_a_cloud(fake_deps, source):
    with pytest.raises(CnsrError, match="--size 只能和 --aws 或 --hetzner 一起用"):
        prepare(fake_deps, size="1c1g", **source)
    assert fake_deps.lightsail.calls == []


def test_a_static_ip_is_for_aws_only(hz):
    with pytest.raises(CnsrError, match="--static-ip 只能和 --aws 一起用"):
        prepare(hz, hetzner="sg", size="1c1g", static_ip="203.0.113.50")


# --- the token -------------------------------------------------------------------------------


def test_the_environment_comes_before_the_saved_token(hz):
    values = localconf.load(hz.deps.conf_path)
    localconf.save(hz.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token"})
    prepare(hz, hetzner="sg", size="1c1g")
    assert hz.calls["make_hetzner"] == [("sg", ENV_TOKEN)]


def test_the_saved_token_comes_next(fake_deps):
    values = localconf.load(fake_deps.deps.conf_path)
    localconf.save(fake_deps.deps.conf_path, {**values, "HCLOUD_TOKEN": "hz-saved-token"})
    prepare(fake_deps, hetzner="sg", size="1c1g")
    assert fake_deps.calls["make_hetzner"] == [("sg", "hz-saved-token")]
    assert "HCLOUD_TOKEN" not in fake_deps.terminal.prompts


def test_a_typed_token_is_checked_then_saved(fake_deps):
    typed = "hz-typed-token-0001"
    fake_deps.terminal.inputs = [typed]
    ctx = prepare(fake_deps, hetzner="sg", size="1c1g")
    assert fake_deps.terminal.prompts == ["Hetzner Cloud API token："]
    assert "没有找到 Hetzner Cloud API token" in fake_deps.terminal.screen
    assert fake_deps.calls["make_hetzner"] == [("sg", typed)]
    assert fake_deps.hetzner.names()[:2] == ["offers", "pick"]  # the check comes before anything else
    assert localconf.load(fake_deps.deps.conf_path)["HCLOUD_TOKEN"] == typed
    assert localconf.load(fake_deps.deps.conf_path)["CLOUDFLARE_API_TOKEN"] == "t-secret"
    assert ctx.hetzner is fake_deps.hetzner


def test_a_token_that_does_not_work_is_not_saved(fake_deps, monkeypatch):
    def refused():
        raise CnsrError("Hetzner API 请求失败（HTTP 401）：token 无效")

    monkeypatch.setattr(fake_deps.hetzner, "offers", refused)
    fake_deps.terminal.inputs = ["hz-bad-token"]
    with pytest.raises(CnsrError, match="HTTP 401"):
        prepare(fake_deps, hetzner="sg", size="1c1g")
    assert "HCLOUD_TOKEN" not in localconf.load(fake_deps.deps.conf_path)


def test_the_token_is_never_shown_or_logged(fake_deps):
    typed = "hz-typed-token-0002"
    fake_deps.terminal.inputs = [typed]
    prepare(fake_deps, hetzner="sg", size="1c1g")
    assert typed not in fake_deps.terminal.text


def test_a_rerun_reads_the_saved_token_without_asking(fake_deps):
    fake_deps.terminal.inputs = ["hz-typed-token-0003"]
    prepare(fake_deps, hetzner="sg", size="1c1g").save()
    prepare(fake_deps)  # no answer queued
    assert fake_deps.calls["make_hetzner"][-1] == ("sg", "hz-typed-token-0003")


# --- the equivalent command ------------------------------------------------------------------


def test_the_command_names_the_region_and_the_size_picked(hz):
    hz.terminal.inputs = ["3", "2", "5", "2"]  # source: Hetzner, regular, region: de-nuremberg, cpx22
    prepare(hz)
    assert equivalent(hz) == ["cnsr up jp-1 --hetzner de-nuremberg --size cpx22 --to xray"]


def test_the_command_with_the_arguments_and_the_hardening_alone(hz):
    prepare(hz, hetzner="sg", size="1c1g", features=None, to="base")
    assert equivalent(hz) == ["cnsr up jp-1 --hetzner sg --size 1c1g --to base"]


def test_the_command_holds_no_token(fake_deps):
    typed = "hz-typed-token-0004"
    fake_deps.terminal.inputs = [typed]
    prepare(fake_deps, hetzner="sg", size="1c1g")
    (line,) = equivalent(fake_deps)
    assert line == "cnsr up jp-1 --hetzner sg --size 1c1g --to xray"
    assert typed not in line and "t-secret" not in line


# --- the record and reruns -------------------------------------------------------------------


def test_the_record_holds_the_size_and_is_stored_from_the_start(hz):
    ctx = prepare(hz, hetzner="us-oregon", size="4c8g")
    assert (ctx.node.source, ctx.node.region, ctx.node.size) == ("hetzner", "us-oregon", "4c8g")
    ctx.save()  # before any takeover check, as for AWS
    assert hz.node().size == "4c8g"


def test_a_rerun_keeps_the_recorded_size_unasked(hz):
    store(hz, size="2c4g", ssh=SshInfo("admin", 40022, "jp-1"))
    ctx = prepare(hz, hetzner="sg")  # no answer queued
    assert ctx.req.size == "2c4g" and ctx.offer is None
    assert hz.hetzner.calls == [] and equivalent(hz) == []
    assert (ctx.req.user, ctx.req.port) == ("admin", 40022)


def test_a_rerun_without_a_login_on_record_uses_root(hz):
    store(hz)
    ctx = prepare(hz)
    assert (ctx.req.user, ctx.req.port) == ("root", 22)


@pytest.mark.parametrize(
    "options",
    [{"hetzner": "de-nuremberg"}, {"hetzner": "sg", "size": "2c4g"}, {"aws": "sg"}, {"ip": "198.51.100.7"}],
)
def test_a_rerun_with_other_arguments_stops(hz, options):
    store(hz)
    with pytest.raises(CnsrError, match=r"服务器 jp-1 已存在（Hetzner sg 1c1g），和这次的参数不一致"):
        prepare(hz, **options)


def test_hetzner_for_an_aws_alias_stops(hz):
    store(hz, source="aws", region="sg", size=None)
    with pytest.raises(CnsrError, match=r"已存在（AWS sg 2c0.5g）"):
        prepare(hz, hetzner="sg")


def test_the_same_arguments_again_are_fine(hz):
    store(hz)
    assert prepare(hz, hetzner="sg", size="1c1g").req.size == "1c1g"


def test_an_alias_naming_another_host_stops(hz):
    write_block(hz.deps.ssh_config, HostBlock("jp-1", "198.51.100.9", "root", 22, "/k.pub"))
    with pytest.raises(CnsrError, match="而这次要在 Hetzner Cloud 新开一台服务器"):
        prepare(hz, hetzner="sg", size="1c1g")
    assert hz.calls["make_hetzner"] == []



def test_back_from_the_types_returns_to_the_locations(hz):
    hz.terminal.inputs = ["3", "", "5", "0", "4", ""]  # Hetzner, regular, de-nuremberg, back, de-falkenstein, first
    ctx = prepare(hz)
    screen = hz.terminal.screen
    assert screen.count("Hetzner 区域\n") == 2
    assert (ctx.req.region, ctx.req.size) == ("de-falkenstein", "cpx02")



# --- the plan first, as in Hetzner's console ---------------------------------------------------


def cost_only_in_helsinki(hz):
    others = [o for o in hz.hetzner.stock if o.plan != "cost_optimized"]
    hz.hetzner.stock_at = {code: others for code in ("sg", "us-oregon", "us-virginia", "de-falkenstein", "de-nuremberg")}


def test_the_plans_come_first_and_say_where_they_are_on_sale(hz):
    cost_only_in_helsinki(hz)
    hz.terminal.inputs = ["3", "1", "", ""]  # Hetzner, Cost-Optimized, the only location, cx23
    ctx = prepare(hz)
    screen = hz.terminal.screen
    assert screen.index("Hetzner 方案") < screen.index("Hetzner 区域") < screen.index("Hetzner 机型")
    plans = screen.split("Hetzner 方案\n")[1].split("Hetzner 区域")[0]
    assert "1 个位置有货" in plans.splitlines()[0] and "6 个位置有货" in plans.splitlines()[1]
    places = screen.split("Hetzner 区域\n")[1].split("Hetzner 机型")[0]
    assert "1. sg（新加坡）（该方案在这里暂无货）" in places and "6. fi-helsinki（芬兰赫尔辛基）  1 种机型" in places
    assert (ctx.req.region, ctx.offer.name) == ("fi-helsinki", "cx23")


def test_a_plan_on_sale_nowhere_cannot_be_picked(hz):
    hz.hetzner.stock = [o for o in hz.hetzner.stock if o.plan != "cost_optimized"]
    hz.terminal.inputs = ["3", "1", "", "", ""]  # Cost-Optimized refused, then the defaults
    prepare(hz)
    assert "1. Cost-Optimized       共享 vCPU，较旧硬件，最便宜，常缺货（所有位置暂无货）" in hz.terminal.screen
    assert "第 1 项所有位置暂无货，不能选，请选别的。" in hz.terminal.screen


def test_back_from_the_locations_returns_to_the_plans_and_from_the_plans_to_the_source(hz):
    hz.terminal.inputs = ["3", "", "0", "0", "3", "3", "", ""]  # Hetzner, regular, back, back, Hetzner, dedicated...
    ctx = prepare(hz)
    screen = hz.terminal.screen
    assert screen.count("服务器来源\n") == 2 and screen.count("Hetzner 方案\n") == 3
    assert ctx.offer.name == "ccx13"
