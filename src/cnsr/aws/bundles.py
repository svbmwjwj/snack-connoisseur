"""Lightsail's bundles in one region, as ``Offer`` values.

The candidates are the active Linux bundles with a public IPv4 address; the IPv6-only ones
cannot be a node. A bundle's price already holds its IPv4 address, and a static IP attached
to an instance costs nothing more. Lightsail does not say what traffic beyond the bundle's
costs, so the offers leave it out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from cnsr.sizes import Offer, label

# The sizes the menu shows first, and its default (nano_3_0, the bundle cnsr always used).
FEATURED = ("2c0.5g", "2c1g", "2c2g")
DEFAULT_SIZE = "2c0.5g"

_GB = 1024**3


def offers(bundles: list[dict[str, Any]]) -> list[Offer]:
    """Every bundle a node can be, each under the size that names it exactly.

    ``bundles`` is the list from ``get_bundles``.
    """
    return [_offer(bundle) for bundle in bundles if _usable(bundle)]


def _usable(bundle: dict[str, Any]) -> bool:
    if not bundle.get("isActive", True) or "LINUX_UNIX" not in (bundle.get("supportedPlatforms") or []):
        return False
    count = bundle.get("publicIpv4AddressCount")
    return count != 0 if count is not None else "ipv6" not in str(bundle.get("bundleId"))


def _offer(bundle: dict[str, Any]) -> Offer:
    cores, memory = bundle.get("cpuCount", 0), bundle.get("ramSizeInGb", 0)
    return Offer(
        size=label(cores, memory),
        name=str(bundle.get("bundleId")),
        cores=cores,
        memory=memory,
        disk=bundle.get("diskSizeInGb", 0),
        included_traffic=int(bundle.get("transferPerMonthInGb", 0) * _GB),
        price_per_tb=None,
        monthly=Decimal(str(bundle.get("price", 0))),
        ip_monthly=Decimal(0),
        currency="USD",
    )
