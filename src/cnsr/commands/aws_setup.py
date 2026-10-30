"""What ``cnsr up`` asks for a new AWS node: the region and the credentials (``size_menu`` the size).

Stopping at any of these questions leaves nothing in Lightsail.
"""

from __future__ import annotations

from cnsr import localconf
from cnsr.aws.lightsail import FEATURED_REGIONS, OPT_IN, REGION_NAMES, REGIONS, Lightsail
from cnsr.deploy.context import Deps, known_lightsail

REGION_PROMPT = "AWS 区域"
MORE = "更多地区…"


def choose_region(deps: Deps) -> str:
    """The region picked: one of the featured ones, else any of them under "more regions"."""
    featured, codes = list(FEATURED_REGIONS), list(REGIONS)
    labels = [_label(code) for code in featured], [_label(code) for code in codes]
    first, index = deps.ui.choose_or_more(REGION_PROMPT, labels[0], MORE, labels[1])
    return featured[index] if first else codes[index]


def _label(code: str) -> str:
    enable = "，需先在 AWS 账户里启用" if code in OPT_IN else ""
    return f"{code}（{REGION_NAMES[code]}{enable}）"


def lightsail_for(deps: Deps, region: str) -> Lightsail:
    """Lightsail for ``region``: boto3's default credentials first, then saved or typed keys.

    Typed keys are saved only after a read-only call shows that they work.
    """
    known = known_lightsail(deps, region)
    if known is not None:
        return known
    ui = deps.ui
    ui.info("没有找到 AWS 凭据（环境变量和 ~/.aws 里都没有）。请输入用于 Lightsail 的 access key，cnsr 会把它保存在本机。")
    keys = (ui.ask("AWS access key ID", sensitive=True), ui.secret("AWS secret access key"))
    lightsail = deps.make_lightsail(region, keys)
    lightsail.pick_blueprint()
    values = localconf.load(deps.conf_path)
    localconf.save(deps.conf_path, {**values, "AWS_ACCESS_KEY_ID": keys[0], "AWS_SECRET_ACCESS_KEY": keys[1]})
    return lightsail
