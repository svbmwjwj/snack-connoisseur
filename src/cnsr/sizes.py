"""Server sizes: what a cloud sells in one place, and the cheapest one that meets a minimum.

A size names its minimum: ``1c1g`` is at least 1 core and 1 GB of memory, ``2c0.5g`` at
least 2 cores and half a GB. Each cloud turns its catalog into ``Offer`` values; picking and
listing work the same for all of them.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_TB = 1024**4
_SIZE = re.compile(r"(\d+)c(\d+(?:\.\d+)?)g")
_CURRENCIES = {"EUR": "欧元", "USD": "美元"}


@dataclass(frozen=True)
class Offer:
    """A server type as size ``size`` would buy it in one place.

    ``memory`` and ``disk`` are in GB, ``included_traffic`` in bytes; the prices are gross
    amounts in ``currency``. ``price_per_tb`` is None when the cloud does not say it. ``plan``
    is the cloud's class of the type (Hetzner's category), empty when it has none.
    """

    size: str
    name: str
    cores: float
    memory: float
    disk: float
    included_traffic: int
    price_per_tb: Decimal | None
    monthly: Decimal
    ip_monthly: Decimal
    currency: str
    plan: str = ""

    @property
    def total_monthly(self) -> Decimal:
        """The server and its IPv4 address, per month."""
        return self.monthly + self.ip_monthly


def parse(size: str) -> tuple[int, float] | None:
    """The minimum cores and GB of memory ``size`` names; None when it is not ``<cores>c<GB>g``."""
    found = _SIZE.fullmatch(size)
    return None if found is None else (int(found[1]), float(found[2]))


def label(cores: float, memory: float) -> str:
    """The size that names exactly ``cores`` and ``memory``: 2, 0.5 is ``2c0.5g``."""
    return f"{cores:g}c{memory:g}g"


def pick(catalog: list[Offer], size: str) -> Offer | None:
    """The cheapest offer that meets ``size``, as that size; None when none does."""
    cores, memory = parse(size)
    meets = [offer for offer in catalog if offer.cores >= cores and offer.memory >= memory]
    if not meets:
        return None
    return dataclasses.replace(min(meets, key=lambda offer: (offer.monthly, offer.name)), size=size)


def every(catalog: list[Offer]) -> list[Offer]:
    """Every size the catalog sells, one offer each under its own size, cheapest first.

    An offer that its own size would not buy (a cheaper one meets it too) is left out, so
    that ``--size`` with any listed size buys the offer listed.
    """
    listed = {}
    for offer in catalog:
        size = label(offer.cores, offer.memory)
        if size not in listed and pick(catalog, size).name == offer.name:
            listed[size] = dataclasses.replace(offer, size=size)
    return sorted(listed.values(), key=lambda offer: (offer.total_monthly, offer.cores, offer.memory))


def describe(offer: Offer) -> str:
    """One line with the type, its configuration, traffic and monthly price, for messages."""
    size, name, cores, memory, disk, traffic, price = columns(offer)
    return f"{size}  {name}  {cores} / {memory} / {disk}  {traffic}  约 {price}"


def menu_line(offer: Offer) -> str:
    """``describe`` as a menu option: the columns of ``columns``, split by tabs to line up."""
    return "\t".join(columns(offer))


def columns(offer: Offer) -> list[str]:
    """The cores and memory as a size, the type, cores, memory, disk, traffic and monthly price."""
    unit = _CURRENCIES.get(offer.currency, offer.currency)
    extra = "另计" if offer.price_per_tb is None else f" {money(offer.price_per_tb)} {unit}/TB"
    return [
        label(offer.cores, offer.memory), offer.name, f"{offer.cores:g} 核", f"{offer.memory:g} GB 内存", f"{offer.disk:g} GB 磁盘",
        f"含 {terabytes(offer.included_traffic)} TB 流量，超出{extra}", f"{money(offer.total_monthly)} {unit}/月",
    ]  # fmt: skip


def money(amount: Decimal) -> str:
    """``amount`` to the cent, without trailing zeros: 4.59, 4.5, 1."""
    text = f"{amount.quantize(Decimal('0.01'), ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def terabytes(size: int) -> str:
    """Bytes as terabytes (1024**4 bytes), with at most one decimal."""
    return f"{round(size / _TB, 1):g}"
