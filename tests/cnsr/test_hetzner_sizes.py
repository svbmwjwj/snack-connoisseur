"""Hetzner sizes: which server types a size buys in a location, and how they are shown."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cnsr.errors import CnsrError
from cnsr.hetzner import sizes
from cnsr.hetzner.sizes import offers
from cnsr.sizes import Offer, describe, money, parse
from hetzner_harness import TB, price, pricing, stype

PAST = {"announced": "2000-01-01T00:00:00+00:00", "unavailable_after": "2000-04-01T00:00:00+00:00"}
FUTURE = {"announced": "2000-01-01T00:00:00+00:00", "unavailable_after": "2999-01-01T00:00:00+00:00"}


def names(found: list[Offer], size: str) -> list[str]:
    cores, memory = parse(size)
    meets = [offer for offer in found if offer.cores >= cores and offer.memory >= memory]
    return [offer.name for offer in sorted(meets, key=lambda offer: (offer.monthly, offer.name))]


def test_the_plans_are_hetzners_three_categories_in_console_order():
    assert list(sizes.PLANS) == ["cost_optimized", "regular_purpose", "general_purpose"]
    assert sizes.DEDICATED == "general_purpose"


def test_each_tier_lists_the_types_that_meet_it_cheapest_first():
    types = [
        stype("cpx22", 2, 4, "9.0000"),
        stype("cpx02", 1, 1, "4.0000"),
        stype("cpx42", 8, 16, "30.0000"),
        stype("cpx12", 1, 2, "6.0000"),
        stype("cpx32", 4, 8, "16.0000"),
    ]
    found = offers(types, pricing(), "fsn1")
    assert names(found, "1c1g") == ["cpx02", "cpx12", "cpx22", "cpx32", "cpx42"]
    assert names(found, "2c4g") == ["cpx22", "cpx32", "cpx42"]
    assert names(found, "4c8g") == ["cpx32", "cpx42"]


def test_a_type_must_meet_both_cores_and_memory():
    types = [stype("many-cores", 4, 2), stype("much-memory", 1, 8)]
    found = offers(types, pricing(), "fsn1")
    assert names(found, "2c4g") == []
    assert names(found, "1c1g") == ["many-cores", "much-memory"]


def test_equal_prices_sort_by_name():
    found = offers([stype("b", 2, 2, "5"), stype("a", 2, 2, "5.00")], pricing(), "fsn1")
    assert names(found, "1c1g") == ["a", "b"]


def test_prices_compare_as_numbers_not_text():
    found = offers([stype("ten", 2, 2, "10.0000"), stype("nine", 2, 2, "9.0000")], pricing(), "fsn1")
    assert names(found, "1c1g") == ["nine", "ten"]


@pytest.mark.parametrize("change", [
    {"architecture": "arm"},
    {"available": False},
    {"deprecation": PAST},
    {"place_deprecation": PAST},
])
def test_types_not_sold_here_now_are_left_out(change):
    found = offers([stype("odd", 2, 4, **change), stype("fine", 2, 4)], pricing(), "fsn1")
    assert names(found, "1c1g") == ["fine"]


def test_dedicated_types_are_kept_under_their_plan():
    found = offers([stype("ccx13", 2, 8, cpu_type="dedicated", category="general_purpose")], pricing(), "fsn1")
    assert [(offer.name, offer.plan) for offer in found] == [("ccx13", "general_purpose")]


def test_an_announced_deprecation_still_on_sale_is_kept():
    found = offers([stype("leaving", 2, 4, deprecation=FUTURE, place_deprecation=FUTURE)], pricing(), "fsn1")
    assert names(found, "1c1g") == ["leaving"]


def test_the_legacy_deprecated_flag_is_ignored():
    found = offers([stype("flagged", 2, 4, deprecated=True)], pricing(), "fsn1")
    assert names(found, "1c1g") == ["flagged"]


def test_a_type_sold_only_elsewhere_or_without_a_price_here_is_left_out():
    elsewhere = stype("elsewhere", 2, 4, location="hel1")
    no_price = stype("no-price", 2, 4)
    no_price["prices"] = [price("hel1", "1")]
    assert names(offers([elsewhere, no_price], pricing(), "fsn1"), "1c1g") == []


def test_each_offer_is_sized_by_its_type_name():
    found = offers([stype("cpx02", 1, 1), stype("cx23", 2, 4, category="cost_optimized")], pricing(), "fsn1")
    assert [(offer.size, offer.plan) for offer in found] == [("cpx02", "regular_purpose"), ("cx23", "cost_optimized")]


def test_an_offer_carries_the_configuration_and_gross_prices():
    (offer,) = offers([stype("cpx02", 1, 1, "4.1300", disk=20)], pricing(ip="0.6050"), "fsn1")
    assert offer == Offer(
        size="cpx02", name="cpx02", cores=1, memory=1, disk=20, included_traffic=20 * TB,
        price_per_tb=Decimal("1.1900"), monthly=Decimal("4.1300"), ip_monthly=Decimal("0.6050"), currency="EUR",
        plan="regular_purpose",
    )
    assert offer.total_monthly == Decimal("4.7350")


def test_a_location_without_an_ipv4_price_is_an_error():
    with pytest.raises(CnsrError, match="IPv4 Primary IP 价格"):
        offers([stype("cpx02", 1, 1)], pricing(location="hel1"), "fsn1")


def test_describe_shows_configuration_traffic_and_total_price():
    offer = Offer("1c1g", "cpx02", 1, 1, 20, 20 * TB, Decimal("1.19"), Decimal("4.1300"), Decimal("0.605"), "EUR")
    assert describe(offer) == (
        "1c1g  cpx02  1 核 / 1 GB 内存 / 20 GB 磁盘  含 20 TB 流量，超出 1.19 欧元/TB  约 4.74 欧元/月"
    )


def test_describe_shows_fractions_of_a_terabyte_and_gigabyte():
    offer = Offer("1c1g", "x", 2, 0.5, 40, TB // 2, Decimal("1"), Decimal("4"), Decimal("1"), "USD")
    assert "2 核 / 0.5 GB 内存 / 40 GB 磁盘  含 0.5 TB 流量，超出 1 美元/TB  约 5 美元/月" in describe(offer)


@pytest.mark.parametrize("amount,text", [("4.5900", "4.59"), ("4.5000", "4.5"), ("1.0000", "1"), ("100", "100"),
                                         ("0.605", "0.61")])
def test_money(amount, text):
    assert money(Decimal(amount)) == text


# --- through the API


def answer_types(hz_api, *types, ip="0.6050"):
    hz_api.on("GET", "/server_types", {"server_types": list(types), "meta": {"pagination": {"next_page": None}}})
    hz_api.on("GET", "/pricing", {"pricing": pricing(ip=ip)})


def test_hetzner_offers_reads_types_and_pricing_for_its_location(hz_api):
    answer_types(hz_api, stype("cpx12", 1, 2, "6"), stype("cpx02", 1, 1, "4"))
    found = hz_api.hz.offers()
    assert names(found, "1c1g") == ["cpx02", "cpx12"]
    assert hz_api.paths() == ["GET /server_types", "GET /pricing"]


def test_pick_returns_the_cheapest(hz_api):
    answer_types(hz_api, stype("cpx12", 1, 2, "6"), stype("cpx02", 1, 1, "4"))
    assert hz_api.hz.pick("1c1g").name == "cpx02"


def test_pick_without_stock_names_the_tier_and_region(hz_api):
    answer_types(hz_api, stype("cpx02", 1, 1, "4"), stype("cpx22", 2, 4, available=False))
    with pytest.raises(CnsrError) as caught:
        hz_api.hz.pick("2c4g")
    assert "2c4g" in str(caught.value) and "至少 2 核 4 GB" in str(caught.value)
    assert "de-falkenstein（德国法尔肯施泰因）" in str(caught.value)
