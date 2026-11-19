"""Table tests for R4, R8, R15, R22 and R25, and the registry as a whole: every rule of the
catalogue (R1 to R26) is registered and cites a clause the product config names."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.integrations.core.contract import MemberPolicy
from app.services.rules.contract import (
    DeductionCitation,
    Facts,
    Head,
    HospitalFacts,
    LineFacts,
    ProductConfig,
    RoomFacts,
    RulesEngine,
)
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import Code, RuleResult
from tests.services.test_rules import policy

UIN = "PLACEHOLDER-AROGYA-SANJEEVANI-UIN"
D = Decimal
CATALOGUE = [f"R{n}" for n in range(1, 27)]


@pytest.fixture
def engine() -> RulesEngine:
    return DefaultRulesEngine(Settings().config_dir / "products")


@pytest.fixture
def product(engine: RulesEngine) -> ProductConfig:
    return engine.product(UIN)


def one(engine: RulesEngine, rule_id: str, facts: Facts, product: ProductConfig) -> RuleResult:
    [result] = engine.evaluate([rule_id], facts, product)
    assert result.rule_id == rule_id
    return result


def test_every_rule_of_the_catalogue_is_registered(
    engine: RulesEngine, product: ProductConfig
) -> None:
    results = engine.evaluate(CATALOGUE, Facts(), product)

    assert [r.rule_id for r in results] == CATALOGUE


# --- R4: hospital registered, and in the network for a cashless claim ----------------------

SUNRISE = HospitalFacts(rohini_id="8900000000011", network=True)
OUTSIDE = HospitalFacts(rohini_id="8900000000099", network=False)
UNKNOWN = HospitalFacts(rohini_id=None, network=False)


@pytest.mark.parametrize(
    ("hospital", "route", "passed", "words"),
    [
        (SUNRISE, "cashless", True, "in the network"),
        (SUNRISE, "reimbursement", True, "registered"),
        (OUTSIDE, "reimbursement", True, "outside the network"),
        (OUTSIDE, "cashless", False, "network hospital"),
        (UNKNOWN, "reimbursement", False, "not in the hospital register"),
        (UNKNOWN, "cashless", False, "not in the hospital register"),
    ],
    ids=[
        "network_cashless",
        "network_reimbursement",
        "non_network_reimbursement",
        "non_network_cashless",
        "unknown_reimbursement",
        "unknown_cashless",
    ],
)
def test_r4_hospital(
    engine: RulesEngine,
    product: ProductConfig,
    hospital: HospitalFacts,
    route: Any,
    passed: bool,
    words: str,
) -> None:
    result = one(engine, "R4", Facts(hospital=hospital, claim_route=route), product)

    assert (result.passed, result.clause_ref) == (passed, "HOSPITAL")
    assert words in result.reason
    assert result.amount_effect == 0


def test_r4_needs_the_hospital_and_the_route(engine: RulesEngine, product: ProductConfig) -> None:
    result = one(engine, "R4", Facts(claim_route="cashless"), product)

    assert not result.passed and "hospital" in result.reason
    result = one(engine, "R4", Facts(hospital=SUNRISE), product)
    assert not result.passed and "claim_route" in result.reason


# --- R8: 60-month moratorium on contesting non-disclosure --------------------------------


def covered(start: date, **changes: Any) -> MemberPolicy:
    return policy(inception_date=start, continuous_cover_start=start, **changes)


@pytest.mark.parametrize(
    ("cover_start", "onsets", "declared", "passed", "words"),
    [
        (date(2024, 1, 1), [], [], True, "no undisclosed"),
        (date(2024, 1, 1), [date(2023, 6, 1)], [], False, "can be contested"),
        (date(2020, 1, 1), [date(2019, 6, 1)], [], True, "cannot be contested"),
        (date(2024, 1, 1), [date(2023, 6, 1)], ["K35"], True, "no undisclosed"),
        (date(2024, 1, 1), [date(2025, 6, 1)], [], True, "no undisclosed"),
    ],
    ids=[
        "nothing_found",
        "undisclosed_within_moratorium",
        "undisclosed_after_60_months",
        "declared",
        "started_after_inception",
    ],
)
def test_r8_moratorium(
    engine: RulesEngine,
    product: ProductConfig,
    cover_start: date,
    onsets: list[date],
    declared: list[str],
    passed: bool,
    words: str,
) -> None:
    peds = [Code(system="ICD-10-CM", version="FY2027", code=c) for c in declared]
    facts = Facts(
        policy=covered(cover_start, declared_peds=peds),
        admission_date=date(2026, 11, 2),
        pre_existing_onsets=onsets,
        diagnosis_codes=["K35.890"],
    )
    result = one(engine, "R8", facts, product)

    assert (result.passed, result.clause_ref) == (passed, "MORATORIUM")
    assert words in result.reason


def test_r8_counts_the_moratorium_from_continuous_cover(
    engine: RulesEngine, product: ProductConfig
) -> None:
    """Exactly 60 months of continuous cover on the admission day is in the moratorium."""
    facts = Facts(
        policy=covered(date(2021, 11, 2)),
        admission_date=date(2026, 11, 2),
        pre_existing_onsets=[date(2021, 1, 1)],
        diagnosis_codes=["E11.9"],
    )
    assert one(engine, "R8", facts, product).passed
    facts = facts.model_copy(update={"admission_date": date(2026, 11, 1)})
    assert not one(engine, "R8", facts, product).passed


# --- R15: proportionate deduction, split from the room cap (R13) ---------------------------

WORKED_LINES = [
    LineFacts(line_id=1, head=Head.ROOM, description="Deluxe room", qty=D(4), rate=D(7500),
              claimed=D("30000")),
    LineFacts(line_id=2, head=Head.PROFESSIONAL_FEES, description="Surgeon fee",
              claimed=D("30000")),
    LineFacts(line_id=3, head=Head.OT, description="OT charges", claimed=D("20000")),
    LineFacts(line_id=4, head=Head.NURSING, description="Nursing", claimed=D("10000")),
    LineFacts(line_id=5, head=Head.PHARMACY, description="Drugs", claimed=D("25000")),
    LineFacts(line_id=6, head=Head.IMPLANTS, description="Plate and screws", claimed=D("40000")),
    LineFacts(line_id=7, head=Head.DIAGNOSTICS, description="CT scan", claimed=D("15000")),
]  # fmt: skip
ROOM = RoomFacts(sum_insured=D("300000"), room_rate_per_day=D("7500"), differential_billing=True)


def test_r13_caps_only_room_lines_and_r15_reduces_the_linked_heads(
    engine: RulesEngine, product: ProductConfig
) -> None:
    r13 = [one(engine, "R13", Facts(line=ln, room=ROOM), product) for ln in WORKED_LINES]
    r15 = [one(engine, "R15", Facts(line=ln, room=ROOM), product) for ln in WORKED_LINES]

    assert [r.amount_effect for r in r13] == [D("-10000.00")] + [D("0")] * 6
    assert [r.amount_effect for r in r15] == [
        D("0"),
        D("-10000.00"),
        D("-6666.67"),
        D("-3333.33"),
        D("0"),
        D("0"),
        D("0"),
    ]
    assert [r.clause_ref for r in r15[1:4]] == ["PROP-DED"] * 3
    assert r13[0].clause_ref == "ROOM-CAP"
    assert "R15" in r13[1].reason  # R13 points at the rule that reduces the line


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"differential_billing": False}, "no differential billing"),
        ({"room_rate_per_day": D("5000")}, "within its cap"),
        ({"room_rate_per_day": None}, "within its cap"),
    ],
    ids=["one_rate_hospital", "room_at_cap", "no_room_line"],
)
def test_r15_passes_without_a_room_breach(
    engine: RulesEngine, product: ProductConfig, changes: dict[str, Any], words: str
) -> None:
    room = ROOM.model_copy(update=changes)
    result = one(engine, "R15", Facts(line=WORKED_LINES[1], room=room), product)

    assert (result.passed, result.amount_effect) == (True, 0)
    assert words in result.reason


def test_r15_never_reduces_excluded_heads(engine: RulesEngine, product: ProductConfig) -> None:
    for line in WORKED_LINES[4:]:
        result = one(engine, "R15", Facts(line=line, room=ROOM), product)
        assert (result.passed, result.amount_effect) == (True, 0)
        assert "excluded from proportionate deduction" in result.reason


# --- R22: every deduction cites its clause --------------------------------------------------


@pytest.mark.parametrize(
    ("deductions", "passed", "words"),
    [
        ([], True, "no deductions"),
        (
            [DeductionCitation(line_id=1, rule_id="R13", amount=D("10000"), clause_ref="ROOM-CAP")],
            True,
            "every deduction cites a clause",
        ),
        (
            [
                DeductionCitation(line_id=1, rule_id="R13", amount=D("10000"),
                                  clause_ref="ROOM-CAP"),
                DeductionCitation(line_id=3, rule_id=None, amount=D("500"), clause_ref=None,
                                  judged_by="person"),
            ],
            False,
            "line 3",
        ),
    ],
    ids=["none", "all_cited", "a_person_edit_without_a_clause"],
)  # fmt: skip
def test_r22_every_deduction_cites_its_clause(
    engine: RulesEngine,
    product: ProductConfig,
    deductions: list[DeductionCitation],
    passed: bool,
    words: str,
) -> None:
    result = one(engine, "R22", Facts(deductions=deductions), product)

    assert (result.passed, result.amount_effect, result.clause_ref) == (
        passed,
        0,
        "DEDUCTION-REASONS",
    )
    assert words in result.reason


# --- R25: PAN or Aadhaar at Rs 1 lakh claimed -----------------------------------------------


@pytest.mark.parametrize(
    ("claimed", "kyc", "passed", "words"),
    [
        (D("99999.99"), False, True, "below"),
        (D("100000.00"), True, True, "on file"),
        (D("100000.00"), False, False, "PAN or Aadhaar"),
        (D("250000.00"), None, False, "kyc_on_file"),
    ],
    ids=["below_threshold", "at_threshold_with_kyc", "at_threshold_without", "unknown"],
)
def test_r25_kyc_from_one_lakh(
    engine: RulesEngine,
    product: ProductConfig,
    claimed: Decimal,
    kyc: bool | None,
    passed: bool,
    words: str,
) -> None:
    result = one(engine, "R25", Facts(claimed=claimed, kyc_on_file=kyc), product)

    assert result.passed is passed
    assert words in result.reason
    if kyc is not None:
        assert result.clause_ref == "KYC"
