"""What ``cnsr up`` asks for the size of a new cloud server.

On AWS the menu shows the featured sizes first and "more sizes" last, which lists every size
the region sells. Every line shows its configuration, traffic and price, and picking one
accepts that price. A Hetzner type is picked with the plan and location
(``hetzner_setup.choose_server``); here it is only shown. Stopping here leaves nothing bought.
"""

from __future__ import annotations

import re
from typing import Protocol

from cnsr.aws import bundles
from cnsr.aws.lightsail import REGION_NAMES as AWS_REGION_NAMES
from cnsr.errors import CnsrError
from cnsr.hetzner.cloud import REGION_NAMES as HETZNER_REGION_NAMES
from cnsr.sizes import Offer, describe, every, menu_line, parse, pick
from cnsr.ui import Ui

PLACES = {"aws": ("AWS", AWS_REGION_NAMES), "hetzner": ("Hetzner", HETZNER_REGION_NAMES)}
PROMPTS = {"aws": "AWS 规格（月价含 IPv4 地址）", "hetzner": "Hetzner 机型（月价含税，含 IPv4 地址）"}
MORE = "更多规格…"
SOLD_OUT = "该区域暂无货"
_TYPE = re.compile(r"[a-z]+\d+[a-z]*")


class Catalog(Protocol):
    def offers(self) -> list[Offer]: ...

    def pick(self, size: str) -> Offer: ...


def check(size: str, source: str) -> str | None:
    """Why ``size`` is no size of ``source``; None when it is one.

    On AWS it is a minimum, ``<cores>c<GB>g``; on Hetzner a server type's name (``cx23``), or
    a minimum as recorded before types were named.
    """
    if parse(size) is not None or (source == "hetzner" and _TYPE.fullmatch(size)):
        return None
    if source == "hetzner":
        return f"--size 的 {size} 不是 Hetzner 机型：写型号，例如 cx23、cpx22、ccx13。"
    return f"--size 的 {size} 不是规格：写成 <核数>c<内存 GB>g，例如 2c4g、2c0.5g。"


def choose(ui: Ui, cloud: Catalog, source: str, region: str, size: str | None) -> Offer:
    """The server type to buy: the cheapest of ``size`` (``--size``), else of the size picked.

    With ``size`` its line is shown; without it a menu lists the featured sizes, each with
    the type it would buy and the price, then "more sizes". A featured size with nothing to
    buy in the place is listed but cannot be picked. A Hetzner type needs ``size`` (picked in
    ``hetzner_setup``); without a terminal AWS takes its default, the bundle every AWS node had
    before sizes were.
    """
    prompt = PROMPTS[source]
    if size is None and source == "aws" and not ui.interactive:
        size = bundles.DEFAULT_SIZE
    if size is not None:
        offer = cloud.pick(size)
        ui.info(f"{prompt.split('（')[0]}：{describe(offer)}")
        return offer
    if not ui.interactive or source == "hetzner":
        raise CnsrError(f"缺少参数：{prompt}")
    catalog = cloud.offers()
    listed = every(catalog)
    if not listed:
        vendor, names = PLACES[source]
        raise CnsrError(f"{vendor} {region}（{names[region]}）暂时没有能买的机型，请换一个区域，或稍后重跑。")
    featured, default_size = bundles.FEATURED, bundles.DEFAULT_SIZE
    picks = {name: pick(catalog, name) for name in featured}
    options = [menu_line(picks[name]) if picks[name] else _sold_out_line(name) for name in featured]
    disabled = {index: SOLD_OUT for index, name in enumerate(featured) if picks[name] is None}
    order = [featured.index(default_size), *range(len(featured))]
    default = next((index for index in order if index not in disabled), len(featured))  # else "more"
    every_line = [menu_line(offer) for offer in listed]
    first, index = ui.choose_or_more(prompt, options, MORE, every_line, default=default, disabled=disabled)
    return picks[featured[index]] if first else listed[index]


def _sold_out_line(size: str) -> str:
    cores, memory = parse(size)
    return f"{size}  至少 {cores} 核 / {memory:g} GB 内存"
