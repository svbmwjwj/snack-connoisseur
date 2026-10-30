"""What ``cnsr up`` asks for a new Hetzner server: the API token, then the plan, the location
and the server type, in the order of Hetzner's console.

All of it is settled in ``prepare``, before anything is bought: stopping at any of these
questions leaves nothing in Hetzner.
"""

from __future__ import annotations

from cnsr import localconf
from cnsr.deploy.context import Deps, known_hetzner
from cnsr.errors import CnsrError
from cnsr.hetzner.cloud import REGION_NAMES, REGIONS, Hetzner
from cnsr.hetzner.sizes import PLANS
from cnsr.sizes import Offer, menu_line, money

REGION_PROMPT = "Hetzner 区域"
PLAN_PROMPT = "Hetzner 方案"
TYPE_PROMPT = "Hetzner 机型（月价含税，含 IPv4 地址）"
SOLD_OUT = "该区域暂无货"
NOWHERE = "所有位置暂无货"
NOT_HERE = "该方案在这里暂无货"
_UNITS = {"EUR": "欧元", "USD": "美元"}


def choose_server(deps: Deps, region: str | None) -> tuple[str, Offer]:
    """The plan, then the location (unless ``region`` is given), then a server type of that plan
    sold there now; the region and the type picked.

    Each plan says where it is on sale; a plan on sale nowhere (or not in ``region``) and a
    location without the plan are listed but cannot be picked. Esc goes back a level; in the
    plans it goes back to the question before.
    """
    ui = deps.ui
    catalog = hetzner_for(deps, region or next(iter(REGIONS))).catalog()
    places = [region] if region else list(REGIONS)
    plans = list(PLANS) + sorted({o.plan for code in places for o in catalog[code]} - set(PLANS))

    def types(plan: str, code: str) -> list[Offer]:
        return sorted((o for o in catalog[code] if o.plan == plan), key=_price)

    offered = {plan: [code for code in places if types(plan, code)] for plan in plans}
    cheapest = min((o for code in places for o in catalog[code]), key=_price, default=None)
    plan_rows = [_plan_line(plan, offered[plan], [o for code in places for o in types(plan, code)], region) for plan in plans]
    no_plan = {i: (SOLD_OUT if region else NOWHERE) for i, plan in enumerate(plans) if not offered[plan]}
    if cheapest is None:
        where = f"{region}（{REGION_NAMES[region]}）" if region else "所有位置"
        raise CnsrError(f"Hetzner {where}暂时没有能买的机型，请换一个区域，或稍后重跑。")
    default = plans.index(cheapest.plan)
    while True:
        plan = plans[ui.choose(PLAN_PROMPT, plan_rows, default=default, disabled=no_plan)]
        while True:
            code = region or _choose_place(ui, plan, types)
            if code is None:
                break
            listed = types(plan, code)
            index = ui.choose_back(TYPE_PROMPT, [menu_line(offer) for offer in listed])
            if index is not None:
                return code, listed[index]
            if region:
                break


def _choose_place(ui, plan: str, types) -> str | None:
    """The location to buy ``plan`` in; None for "back"."""
    codes = list(REGIONS)
    rows, missing = [], {}
    for index, code in enumerate(codes):
        listed = types(plan, code)
        rows.append(f"{code}（{REGION_NAMES[code]}）" + (f"\t{_offer_count(listed)}" if listed else ""))
        if not listed:
            missing[index] = NOT_HERE
    index = ui.choose_back(REGION_PROMPT, rows, disabled=missing)
    return None if index is None else codes[index]


def _plan_line(plan: str, places: list[str], listed: list[Offer], region: str | None) -> str:
    name = PLANS.get(plan, plan or "其他")
    if not listed:
        return name
    cheapest = min(listed, key=_price)
    unit = _UNITS.get(cheapest.currency, cheapest.currency)
    where = f"{len({o.name for o in listed})} 种机型" if region else f"{len(places)} 个位置有货"
    return f"{name}\t{where}，{money(cheapest.total_monthly)} {unit}/月起"


def _offer_count(listed: list[Offer]) -> str:
    unit = _UNITS.get(listed[0].currency, listed[0].currency)
    return f"{len(listed)} 种机型，{money(listed[0].total_monthly)} {unit}/月起"


def _price(offer: Offer) -> tuple:
    return offer.total_monthly, offer.name


def choose_region(deps: Deps) -> str:
    """The region picked in a menu of the new codes with their Chinese names."""
    codes = list(REGIONS)
    return codes[deps.ui.choose(REGION_PROMPT, [f"{code}（{REGION_NAMES[code]}）" for code in codes])]


def hetzner_for(deps: Deps, region: str) -> Hetzner:
    """Hetzner for ``region``: ``HCLOUD_TOKEN`` in the environment, then the saved token, else a typed one.

    A typed token is saved only after a read-only call shows that it works.
    """
    known = known_hetzner(deps, region)
    if known is not None:
        return known
    ui = deps.ui
    ui.info(
        "没有找到 Hetzner Cloud API token（环境变量 HCLOUD_TOKEN 和本机保存的设置里都没有）。"
        "请在 Hetzner 控制台的项目里生成一个有读写权限的 token，cnsr 会把它保存在本机。"
    )
    token = ui.secret("Hetzner Cloud API token")
    hetzner = deps.make_hetzner(region, token)
    hetzner.offers()  # read-only: a token that does not work stops here, unsaved
    localconf.save(deps.conf_path, {**localconf.load(deps.conf_path), "HCLOUD_TOKEN": token})
    ui.detail(f"Hetzner token 已保存到 {deps.conf_path}。")
    return hetzner

