"""CGHS 2025 rates: rows anchored on the serial number and code, rates read from the right,
and the city tiers with the 09.10.2025 reclassification."""

from decimal import Decimal

from app.knowledge.loaders.cghs import (
    apply_tier_om,
    find_x_cities,
    parse_rate_rows,
    tier_consistency,
    verify_cities,
)
from tests.knowledge import reference_sources as src

D = Decimal


def test_rows_keep_wrapped_names_and_read_the_three_rates_from_the_right() -> None:
    rows = parse_rate_rows([(71, src.CGHS_TIER1_P71)], tier="X")

    assert [(r.serial, r.code) for r in rows] == [(15, "LB012"), (1393, "AG045"), (1997, "HC001")]
    cbc, appendix, check_up = rows
    assert cbc.name.startswith("Complete Haemogram/CBC") and cbc.name.endswith("Examination)")
    assert (cbc.non_nabh, cbc.nabh, cbc.super_speciality) == (D(255), D(300), D(300))
    assert appendix.name == "Appendicectomy"
    assert appendix.specialty == "Abdomen/GI Surgery Procedure"
    assert (appendix.non_nabh, appendix.nabh, appendix.super_speciality) == (
        D(17765),
        D(20900),
        D(24035),
    )
    assert "75 years of age" in check_up.name  # numbers inside a name are not rates
    assert (check_up.nabh, check_up.specialty, check_up.page) == (
        D(2000),
        "Annual Health Check-up",
        71,
    )


def test_tier_rates_follow_the_stated_differentials() -> None:
    x = parse_rate_rows([(71, src.CGHS_TIER1_P71)], tier="X")
    y = parse_rate_rows([(160, src.CGHS_TIER2_P160)], tier="Y")

    consistent, compared = tier_consistency(x, y, D("0.90"))

    assert (consistent, compared) == (1, 1)  # AG045: 20900 x 0.9 = 18810


def test_x_cities_are_read_and_y_cities_are_verified_against_the_page() -> None:
    assert find_x_cities(src.CGHS_CITIES_P4) == [
        "Hyderabad",
        "Delhi",
        "Ahmedabad",
        "Bengaluru",
        "Mumbai",
        "Pune",
        "Chennai",
        "Kolkata",
    ]
    found, missing = verify_cities(
        ["Vijayawada", "Ghaziabad", "Chandigarh", "Atlantis"], src.CGHS_CITIES_P109
    )
    assert (found, missing) == (["Vijayawada", "Ghaziabad", "Chandigarh"], ["Atlantis"])


def test_the_tier_om_moves_ncr_cities_to_x_and_adds_cities_at_y() -> None:
    tiers = {"delhi": "X", "gurgaon": "Y", "noida": "Y", "jalandhar": "Y", "chandigarh": "Y"}

    changed = apply_tier_om(tiers)

    assert tiers["gurgaon"] == tiers["noida"] == "X"
    assert tiers["goa"] == tiers["port blair"] == tiers["panchkula"] == "Y"
    assert tiers["jalandhar cantt"] == "Y"
    assert ("gurgaon", "Y", "X") in changed
