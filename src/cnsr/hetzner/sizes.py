"""Hetzner's catalog in one location: the server types it sells there now, as ``Offer`` values.

The candidates are the x86 server types of every plan that the location sells now: listed as
available there and not past their ``unavailable_after`` date, for the type as a whole or
for the location. An offer's size is the type's name, its plan the type's category. Prices are gross (VAT included) and add the monthly price of the IPv4
Primary IP every node has. The legacy ``deprecated`` flag is not used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cnsr.errors import CnsrError
from cnsr.sizes import Offer

# Hetzner's plans (a server type's category), in the order its console shows them, as the menu
# names them: the plan, then what sets it apart.
PLANS = {
    "cost_optimized": "Cost-Optimized\t共享 vCPU，较旧硬件，最便宜，常缺货",
    "regular_purpose": "Regular Performance\t共享 vCPU，新硬件，性价比高",
    "general_purpose": "Dedicated\t独享 vCPU，性能稳定，较贵",
}
DEDICATED = "general_purpose"


def offers(server_types: list[dict[str, Any]], pricing: dict[str, Any], location: str) -> list[Offer]:
    """Every x86 server type sold in ``location``, each under its own name.

    ``server_types`` is the list from ``GET /server_types``, ``pricing`` the object from
    ``GET /pricing``.
    """
    ip_monthly = _ip_price(pricing, location)
    currency = str(pricing.get("currency") or "EUR")
    now = datetime.now(UTC)
    found = []
    for server_type in server_types:
        price = _sold_at(server_type, location, now)
        if price is not None:
            found.append(_offer(server_type, price, ip_monthly, currency))
    return found


def _sold_at(server_type: dict[str, Any], location: str, now: datetime) -> dict[str, Any] | None:
    """The type's prices in ``location`` when it is a candidate there, else None."""
    if server_type.get("architecture") != "x86":
        return None
    if _gone(server_type.get("deprecation"), now):
        return None
    place = next((entry for entry in server_type.get("locations") or [] if entry.get("name") == location), None)
    if place is None or not place.get("available") or _gone(place.get("deprecation"), now):
        return None
    return next((entry for entry in server_type.get("prices") or [] if entry.get("location") == location), None)


def _gone(deprecation: dict[str, Any] | None, now: datetime) -> bool:
    if not deprecation or not deprecation.get("unavailable_after"):
        return False
    after = datetime.fromisoformat(deprecation["unavailable_after"])
    return (after if after.tzinfo else after.replace(tzinfo=UTC)) <= now


def _offer(server_type: dict[str, Any], price: dict[str, Any], ip_monthly: Decimal, currency: str) -> Offer:
    return Offer(
        size=str(server_type.get("name")),
        name=str(server_type.get("name")),
        cores=server_type.get("cores", 0),
        memory=server_type.get("memory", 0),
        disk=server_type.get("disk", 0),
        included_traffic=int(price.get("included_traffic") or 0),
        price_per_tb=Decimal(str((price.get("price_per_tb_traffic") or {}).get("gross") or 0)),
        monthly=Decimal(str(price["price_monthly"]["gross"])),
        ip_monthly=ip_monthly,
        currency=currency,
        plan=str(server_type.get("category") or ""),
    )


def _ip_price(pricing: dict[str, Any], location: str) -> Decimal:
    for entry in pricing.get("primary_ips") or []:
        if entry.get("type") != "ipv4":
            continue
        for price in entry.get("prices") or []:
            if price.get("location") == location:
                return Decimal(str(price["price_monthly"]["gross"]))
    raise CnsrError(f"Hetzner 没有给出位置 {location} 的 IPv4 Primary IP 价格，无法列出规格，请稍后重试。")

