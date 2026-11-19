"""Arogya Sanjeevani wording: the limits it states, the specified-condition lists, the clause
catalogue, and the check of the product config against the wording."""

from decimal import Decimal

import pytest

from app.config import Settings
from app.knowledge.loaders.policy_wording import (
    CLAUSES,
    check_config,
    find_limits,
    parse_waiting_lists,
)
from app.services.rules.contract import ProductConfig
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import PRODUCT_UIN
from tests.knowledge import reference_sources as src

D = Decimal


@pytest.fixture
def product() -> ProductConfig:
    return DefaultRulesEngine(Settings().config_dir / "products").product(PRODUCT_UIN)


def _pages() -> list[tuple[int, str]]:
    return [
        (6, src.WORDING_BENEFITS_P6),
        (7, src.WORDING_BENEFITS_P7),
        (8, src.WORDING_WAITING_P8),
        (9, src.WORDING_WAITING_P9),
        (18, src.WORDING_COPAY_P18),
    ]


def test_limits_are_read_from_the_wording_with_their_pages() -> None:
    limits = find_limits(_pages())

    assert limits["room_percent"] == (D("2"), 6)
    assert limits["room_max_per_day"] == (D("5000"), 6)
    assert limits["icu_percent"] == (D("5"), 6)
    assert limits["icu_max_per_day"] == (D("10000"), 6)
    assert limits["ambulance_max"] == (D("2000"), 6)
    assert limits["cataract_percent"] == (D("25"), 6)
    assert limits["cataract_max_per_eye"] == (D("40000"), 6)
    assert limits["pre_days"] == (D("30"), 7)
    assert limits["post_days"] == (D("60"), 7)
    assert limits["modern_treatments_percent"] == (D("50"), 7)
    assert limits["ped_months"] == (D("36"), 8)
    assert limits["co_pay_percent"] == (D("5"), 18)


def test_the_specified_conditions_come_with_their_waiting_months() -> None:
    conditions = parse_waiting_lists(_pages())

    assert [(c.item, c.months, c.page) for c in conditions] == [
        ("Benign ENT disorders", 24, 9),
        ("Hernia of all types", 24, 9),
        ("Calculi in urinary system, Gall Bladder and Bile duct, excluding malignancy.", 24, 9),
        ("Treatment for joint replacement unless arising from accident", 36, 9),
    ]


def test_the_config_check_names_each_limit_that_agrees_or_differs(
    product: ProductConfig,
) -> None:
    limits = find_limits(_pages())
    bad = product.model_copy(update={"co_pay": product.co_pay.model_copy(update={"percent": 10})})

    good_lines = check_config(product, limits)
    bad_lines = check_config(bad, limits)

    assert good_lines["co-pay percent"] == "wording 5 p18, config 5: agree"
    assert bad_lines["co-pay percent"] == "wording 5 p18, config 10: DIFFERS"
    assert all(v.endswith("agree") for v in good_lines.values())


def test_every_clause_has_an_id_a_title_and_a_summary_in_our_words() -> None:
    refs = [ref for ref, _, _, _ in CLAUSES]

    assert len(refs) == len(set(refs)) >= 25
    assert all(title and len(summary) > 30 for _, title, summary, _ in CLAUSES)
