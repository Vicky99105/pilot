"""Table tests per rule ID, through the RulesEngine contract and the real product config."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.integrations.core.contract import InsuredMember, MemberPolicy, PremiumPayment
from app.services.rules.contract import (
    Claimant,
    Facts,
    Head,
    LineFacts,
    NonPayableItem,
    ProductConfig,
    RoomFacts,
    RulesEngine,
    UnknownProduct,
    UnknownRule,
)
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import RuleResult

UIN = "PLACEHOLDER-AROGYA-SANJEEVANI-UIN"
D = Decimal
ADMITTED = date(2026, 11, 2)


@pytest.fixture
def engine() -> RulesEngine:
    return DefaultRulesEngine(Settings().config_dir / "products")


@pytest.fixture
def product(engine: RulesEngine) -> ProductConfig:
    return engine.product(UIN)


def policy(**changes: Any) -> MemberPolicy:
    base = MemberPolicy(
        policy_number="P-1",
        product_uin=UIN,
        sum_insured=D("300000.00"),
        cumulative_bonus=D("0.00"),
        utilised=D("0.00"),
        blocked=D("0.00"),
        inception_date=date(2024, 1, 1),
        continuous_cover_start=date(2024, 1, 1),
        policy_start=date(2026, 1, 1),
        policy_end=date(2026, 12, 31),
        premium_payments=[
            PremiumPayment(paid_on=date(2025, 12, 28), amount=D("9000"), status="received")
        ],
        declared_peds=[],
        insured_members=[
            InsuredMember(
                member_id="M-1",
                name="Asha Verma",
                dob=date(1984, 6, 12),
                gender="F",
                relationship="self",
            ),
            InsuredMember(
                member_id="M-2",
                name="Rohit Verma",
                dob=date(1981, 2, 3),
                gender="M",
                relationship="spouse",
            ),
        ],
    )
    return base.model_copy(update=changes)


def one(engine: RulesEngine, rule_id: str, facts: Facts, product: ProductConfig) -> RuleResult:
    [result] = engine.evaluate([rule_id], facts, product)
    assert result.rule_id == rule_id
    assert result.judged_by == "code"
    return result


# --- R1: premium received before cover start ----------------------------------------------


@pytest.mark.parametrize(
    ("payments", "passed"),
    [
        ([PremiumPayment(paid_on=date(2025, 12, 28), amount=D("9000"), status="received")], True),
        ([PremiumPayment(paid_on=date(2026, 1, 1), amount=D("9000"), status="received")], True),
        ([PremiumPayment(paid_on=date(2026, 1, 5), amount=D("9000"), status="received")], False),
        ([PremiumPayment(paid_on=date(2025, 12, 28), amount=D("9000"), status="bounced")], False),
        ([], False),
    ],
    ids=["before", "on_start", "after", "bounced", "none"],
)
def test_r1_premium_received_before_cover(
    engine: RulesEngine, product: ProductConfig, payments: list[PremiumPayment], passed: bool
) -> None:
    result = one(engine, "R1", Facts(policy=policy(premium_payments=payments)), product)

    assert result.passed is passed
    assert result.clause_ref == "64VB"


# --- R2: in force or in grace on admission -------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "admitted", "passed", "clause"),
    [
        ({}, ADMITTED, True, "POLICY-PERIOD"),
        ({}, date(2025, 12, 31), False, "POLICY-PERIOD"),
        ({}, date(2027, 1, 20), True, "GRACE"),  # 20 days after the end; annual grace 30
        ({}, date(2027, 2, 5), False, "GRACE"),  # 36 days after the end
        (  # monthly, 2 instalments paid: paid up to 2026-02-28, grace 15 days
            {
                "premium_frequency": "monthly",
                "premium_payments": [
                    PremiumPayment(paid_on=date(2025, 12, 28), amount=D("800"), status="received"),
                    PremiumPayment(paid_on=date(2026, 1, 30), amount=D("800"), status="received"),
                ],
            },
            date(2026, 3, 10),
            True,
            "GRACE",
        ),
        (
            {
                "premium_frequency": "monthly",
                "premium_payments": [
                    PremiumPayment(paid_on=date(2025, 12, 28), amount=D("800"), status="received"),
                    PremiumPayment(paid_on=date(2026, 1, 30), amount=D("800"), status="received"),
                ],
            },
            date(2026, 3, 20),
            False,
            "GRACE",
        ),
    ],
    ids=["in_force", "before_start", "annual_grace", "after_grace", "monthly_grace", "lapsed"],
)
def test_r2_policy_in_force_or_in_grace(
    engine: RulesEngine,
    product: ProductConfig,
    changes: dict[str, Any],
    admitted: date,
    passed: bool,
    clause: str,
) -> None:
    result = one(engine, "R2", Facts(policy=policy(**changes), admission_date=admitted), product)

    assert (result.passed, result.clause_ref) == (passed, clause)


# --- R3: member match ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claimant", "passed", "reason", "kind"),
    [
        (Claimant(member_id="M-1", name="Someone Else"), True, "exact member ID", "exact_id"),
        (
            Claimant(member_id="M-9", name="Mrs. ASHA VERMA", dob=date(1984, 6, 12)),
            True,
            "name and date of birth",
            "name_dob",
        ),
        (
            Claimant(member_id=None, name="Asha Verma", dob=date(1985, 6, 12)),
            False,
            "doubtful match",
            "doubtful",
        ),
        (
            Claimant(member_id=None, name="Asha Varmani", dob=date(1984, 6, 12)),
            False,
            "doubtful match",
            "doubtful",
        ),
        (Claimant(member_id=None, name="Imran Qureshi"), False, "no matching member", "none"),
    ],
    ids=["exact_id", "name_dob", "dob_differs", "name_close", "no_match"],
)
def test_r3_member_match(
    engine: RulesEngine,
    product: ProductConfig,
    claimant: Claimant,
    passed: bool,
    reason: str,
    kind: str,
) -> None:
    result = one(engine, "R3", Facts(policy=policy(), claimant=claimant), product)
    match = engine.match_member(claimant, policy().insured_members)

    assert result.passed is passed
    assert reason in result.reason
    assert result.clause_ref == "INSURED"
    assert match.kind == kind
    assert (match.member is not None) is passed


# --- R5: 30-day initial wait --------------------------------------------------------------


@pytest.mark.parametrize(
    ("inception", "cover_start", "accident", "passed"),
    [
        (ADMITTED - timedelta(days=10), ADMITTED - timedelta(days=10), False, False),
        (ADMITTED - timedelta(days=10), ADMITTED - timedelta(days=10), True, True),
        (ADMITTED - timedelta(days=30), ADMITTED - timedelta(days=30), False, True),
        (ADMITTED - timedelta(days=29), ADMITTED - timedelta(days=29), False, False),
        (ADMITTED - timedelta(days=10), date(2025, 9, 1), False, True),  # 14 months' cover
    ],
    ids=["within_wait", "accident_exempt", "day_30", "day_29", "continuous_cover"],
)
def test_r5_initial_wait(
    engine: RulesEngine,
    product: ProductConfig,
    inception: date,
    cover_start: date,
    accident: bool,
    passed: bool,
) -> None:
    facts = Facts(
        policy=policy(inception_date=inception, continuous_cover_start=cover_start),
        admission_date=ADMITTED,
        accident=accident,
    )
    result = one(engine, "R5", facts, product)

    assert result.passed is passed
    assert result.clause_ref == "Excl03"


# --- R10: sum insured available ----------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "amount", "passed"),
    [
        ({}, D("140000"), True),
        ({"cumulative_bonus": D("30000"), "utilised": D("200000")}, D("130000"), True),
        ({"utilised": D("250000"), "blocked": D("40000")}, D("20000"), False),
        ({"utilised": D("250000"), "blocked": D("40000")}, D("10000"), True),
    ],
    ids=["plenty", "bonus_counts", "insufficient", "exactly_enough"],
)
def test_r10_sum_insured_available(
    engine: RulesEngine,
    product: ProductConfig,
    changes: dict[str, Any],
    amount: Decimal,
    passed: bool,
) -> None:
    result = one(engine, "R10", Facts(policy=policy(**changes), amount=amount), product)

    assert result.passed is passed
    assert result.clause_ref == "SUM-INSURED"
    assert result.amount_effect == 0


# --- R11: exact non-payable match ---------------------------------------------------------

NON_PAYABLE = [
    NonPayableItem(list_name="I", item_number=1, item="Gloves"),
    NonPayableItem(list_name="I", item_number=2, item="Admission kit"),
    NonPayableItem(
        list_name="III",
        item_number=1,
        item="Sterilisation charges",
        condition="Part of the procedure charges",
    ),
]


@pytest.mark.parametrize(
    ("description", "passed", "clause", "effect"),
    [
        ("GLOVES", False, "ANNEX-I", D("-450.00")),
        ("Admission Kit.", False, "ANNEX-I", D("-450.00")),
        ("Sterilization charges", False, "ANNEX-III", D("-450.00")),
        ("Paracetamol 500 mg", True, None, D("0")),
        ("Surgical gloves and drapes set", True, None, D("0")),
    ],
    ids=["exact", "punctuation", "spelling_variant", "payable", "not_near_exact"],
)
def test_r11_non_payable_match(
    engine: RulesEngine,
    product: ProductConfig,
    description: str,
    passed: bool,
    clause: str | None,
    effect: Decimal,
) -> None:
    line = LineFacts(line_id=7, head=Head.CONSUMABLES, description=description, claimed=D("450"))
    result = one(engine, "R11", Facts(line=line, non_payable=NON_PAYABLE), product)

    assert (result.passed, result.clause_ref, result.amount_effect) == (passed, clause, effect)


def test_r11_pays_an_item_the_product_pays_and_deducts_one_paid_only_with_an_exception(
    engine: RulesEngine, product: ProductConfig
) -> None:
    items = [
        NonPayableItem(
            list_name="IV",
            item_number=18,
            item="URINE BAG",
            condition="Paid where medically necessary, at most one bag in 24 hours",
            payable_in_product="payable_within_limits",
        ),
        NonPayableItem(
            list_name="I",
            item_number=56,
            item="GLOVES",
            condition="Sterile gloves are paid; non-sterile gloves are not",
            payable_in_product="not_payable_unless",
        ),
    ]

    def r11(description: str) -> RuleResult:
        line = LineFacts(
            line_id=1, head=Head.CONSUMABLES, description=description, claimed=D("264")
        )
        return one(engine, "R11", Facts(line=line, non_payable=items), product)

    bag, gloves = r11("Urine bag"), r11("Gloves")

    assert (bag.passed, bag.amount_effect) == (True, D("0"))
    assert "List IV item 18" in bag.reason and "at most one bag" in bag.reason
    assert (gloves.passed, gloves.amount_effect, gloves.clause_ref) == (
        False,
        D("-264.00"),
        "ANNEX-I",
    )
    assert "Sterile gloves are paid" in gloves.reason


# --- R13: room cap and proportionate deduction --------------------------------------------

# Worked example: SI 3 lakh, room Rs 7,500 a day for 4 days against a Rs 5,000 cap.
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
WORKED_ROOM = RoomFacts(
    sum_insured=D("300000"), room_rate_per_day=D("7500"), differential_billing=True
)


def test_r13_and_r21_worked_example_pays_133000(
    engine: RulesEngine, product: ProductConfig
) -> None:
    results = [
        one(engine, "R13", Facts(line=line, room=WORKED_ROOM), product) for line in WORKED_LINES
    ]
    admissible = sum(
        (line.claimed + r.amount_effect for line, r in zip(WORKED_LINES, results, strict=True)),
        D("0"),
    )
    copay = one(engine, "R21", Facts(admissible_total=admissible), product)

    assert [r.amount_effect for r in results] == [
        D("-10000.00"),
        D("-10000.00"),
        D("-6666.67"),
        D("-3333.33"),
        D("0"),
        D("0"),
        D("0"),
    ]
    assert [r.clause_ref for r in results[:4]] == ["ROOM-CAP"] + ["PROP-DED"] * 3
    assert [r.passed for r in results] == [False] * 4 + [True] * 3
    assert admissible == D("140000.00")
    assert (copay.amount_effect, copay.clause_ref, copay.passed) == (D("-7000.00"), "CO-PAY", True)
    assert admissible + copay.amount_effect == D("133000.00")


def test_r13_no_proportionate_deduction_without_differential_billing(
    engine: RulesEngine, product: ProductConfig
) -> None:
    room = WORKED_ROOM.model_copy(update={"differential_billing": False})
    results = [one(engine, "R13", Facts(line=line, room=room), product) for line in WORKED_LINES]

    assert [r.amount_effect for r in results] == [D("-10000.00")] + [D("0")] * 6
    assert "no differential billing" in results[1].reason


@pytest.mark.parametrize(
    ("line", "effect", "clause"),
    [
        (
            LineFacts(line_id=1, head=Head.ROOM, description="General ward", qty=D(3),
                      rate=D(4000), claimed=D("12000")),
            D("0"),
            "ROOM-CAP",
        ),
        (
            # Rs 1 lakh SI: 2% gives a Rs 2,000 cap, below the Rs 5,000 maximum.
            LineFacts(line_id=1, head=Head.ROOM, description="Twin sharing", qty=D(3),
                      rate=D(4000), claimed=D("12000")),
            D("-6000.00"),
            "ROOM-CAP",
        ),
    ],
    ids=["room_under_cap", "two_percent_of_small_si"],  # ICU lines: R14
)  # fmt: skip
def test_r13_daily_caps(
    engine: RulesEngine,
    product: ProductConfig,
    line: LineFacts,
    effect: Decimal,
    clause: str,
) -> None:
    si = D("100000") if line.description == "Twin sharing" else D("300000")
    room = RoomFacts(sum_insured=si, room_rate_per_day=line.rate, differential_billing=True)
    result = one(engine, "R13", Facts(line=line, room=room), product)

    assert (result.amount_effect, result.clause_ref) == (effect, clause)


def test_unknown_rule_and_product_are_refused(engine: RulesEngine, product: ProductConfig) -> None:
    with pytest.raises(UnknownRule):
        engine.evaluate(["R99"], Facts(), product)
    with pytest.raises(UnknownProduct):
        engine.product("NO-SUCH-UIN")


def test_missing_facts_fail_with_a_reason(engine: RulesEngine, product: ProductConfig) -> None:
    results = engine.evaluate(["R1", "R2", "R10", "R13", "R21"], Facts(), product)

    assert all(not r.passed and "missing" in r.reason for r in results)
