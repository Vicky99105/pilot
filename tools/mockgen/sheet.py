"""The deduction sheet bill review should write for any set of lines: the real rules engine,
run in bill review's order (backend/app/services/bill_review/service.py):

1. per line R11, then R12 on the lines R11 matched nothing for (read as the case says), R18;
2. the package: R23, then R20 over the lines it prices (only when a package is mapped);
3. per line R24, then R13, R14 and R15 with the room rate of the room lines outside it;
4. the claim: R19 (ambulance lines), R16, R17, R26, then R21 (co-pay).

Every amount in a mock ground truth comes from here, never typed by hand. tests/tools checks
that it reproduces the tracer and cashless answer keys exactly.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.database.seed_knowledge import NON_PAYABLE_ITEMS
from app.services.rules.contract import (
    ClaimRoute,
    Facts,
    Head,
    LineFacts,
    MappedPackage,
    NonPayableItem,
    NonPayableJudgement,
    PackageFacts,
    PreAuthFacts,
    RoomFacts,
    RulesEngine,
)
from app.shared.seed_ids import PRODUCT_UIN
from app.shared.types import RuleResult

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SheetLine:
    """A line as bill review reads it: a final bill line, or an estimate head."""

    line_id: int
    head: Head
    description: str
    qty: Decimal | None
    rate: Decimal | None
    claimed: Decimal
    service_date: date | None


@dataclass(frozen=True)
class SheetTerms:
    """What the claim brings to bill review besides its lines."""

    route: ClaimRoute
    sum_insured: Decimal
    differential_billing: bool
    packages: list[MappedPackage]  # empty: no package mapped
    non_package_discount: Decimal | None
    procedure_codes: list[str] = field(default_factory=list)
    diagnosis_codes: list[str] = field(default_factory=list)
    # R12's expected reading of a line R11 matched nothing for, by description. A line not
    # named here is read as payable (R12 passes).
    r12_readings: dict[str, NonPayableItem | None] = field(default_factory=dict)
    billed_as_package: bool = False


def seeded_non_payable() -> list[NonPayableItem]:
    return [
        NonPayableItem(list_name=list_name, item_number=number, item=item, condition=condition)
        for list_name, number, item, _, condition in NON_PAYABLE_ITEMS
    ]


def expected_sheet(
    lines: list[SheetLine],
    rules: RulesEngine,
    terms: SheetTerms,
    *,
    admission: date,
    discharge: date | None,
    preauth: PreAuthFacts | None = None,
    non_payable: list[NonPayableItem] | None = None,
    product_uin: str = PRODUCT_UIN,
) -> dict[str, Any]:
    product = rules.product(product_uin)
    non_payable = seeded_non_payable() if non_payable is None else non_payable
    rows: list[dict[str, Any]] = [
        {
            "line_id": ln.line_id,
            "head": ln.head.value,
            "description": ln.description,
            "claimed": ln.claimed,
            "admissible": ln.claimed,
            "deducted": ZERO,
            "rule_id": None,
            "clause_ref": None,
            "reason": None,
            "deductions": [],
            "in_package": False,
        }
        for ln in lines
    ]
    source = {ln.line_id: ln for ln in lines}
    results: list[RuleResult] = []
    route = terms.route

    def line_facts(row: dict[str, Any]) -> LineFacts:
        ln = source[row["line_id"]]
        rate = row["admissible"] / ln.qty if row["deductions"] and ln.qty else ln.rate
        return LineFacts(
            line_id=ln.line_id,
            head=ln.head,
            description=ln.description,
            qty=ln.qty,
            rate=rate,
            claimed=row["admissible"],
            service_date=ln.service_date,
        )

    def apply(row: dict[str, Any], rule_id: str, extra: dict[str, Any]) -> RuleResult:
        [result] = rules.evaluate([rule_id], Facts(line=line_facts(row), **extra), product)
        results.append(result)
        _deduct(row, result, -result.amount_effect)
        return result

    def spread(rule_id: str, targets: list[dict[str, Any]], extra: dict[str, Any]) -> RuleResult:
        [result] = rules.evaluate([rule_id], Facts(**extra), product)
        results.append(result)
        if result.amount_effect and targets:
            weights = [row["admissible"] for row in targets]
            for row, share in zip(
                targets, rules.allocate(result.amount_effect, weights), strict=True
            ):
                _deduct(row, result, -share)
        return result

    def payable() -> list[dict[str, Any]]:
        return [row for row in rows if row["admissible"] > 0]

    def admissible() -> Decimal:
        return money(sum((row["admissible"] for row in rows), ZERO))

    # 1. Per line.
    unmatched = [
        r for r in rows if apply(r, "R11", {"non_payable": non_payable}).clause_ref is None
    ]
    for row in unmatched:
        if row["admissible"] > 0 and terms.r12_readings.get(row["description"]) is not None:
            reading = terms.r12_readings[row["description"]]
            apply(row, "R12", {"judgement": NonPayableJudgement(item=reading, confidence=1.0)})
    for row in payable():
        apply(row, "R18", {"admission_date": admission, "discharge_date": discharge})

    # 2. The package.
    package: PackageFacts | None = None
    r20: RuleResult | None = None
    bound = route in product.packages.pricing_applies_to
    if terms.packages:
        package = PackageFacts(packages=terms.packages, billed_as_package=terms.billed_as_package)
        for row in rows:
            head = Head(row["head"])
            row["in_package"] = bound and (
                head is Head.PACKAGE or head in product.packages.included_heads
            )
        for row in payable():
            apply(row, "R23", {"package": package, "claim_route": route})
        priced = [
            row
            for row in payable()
            if row["in_package"]
            and (Head(row["head"]) is Head.PACKAGE or not terms.billed_as_package)
        ]
        total = sum((row["admissible"] for row in priced), ZERO)
        r20 = spread(
            "R20",
            priced,
            {"package": package.model_copy(update={"package_total": total}), "claim_route": route},
        )

    # 3. Per line outside the package.
    for row in payable():
        apply(
            row,
            "R24",
            {
                "package": package,
                "non_package_discount_percent": terms.non_package_discount,
                "claim_route": route,
            },
        )
    room_rows = [r for r in payable() if r["head"] == Head.ROOM.value and not r["in_package"]]
    days = sum((source[r["line_id"]].qty or Decimal("1") for r in room_rows), ZERO)
    room_rate = (
        sum((r["admissible"] for r in room_rows), ZERO) / days if room_rows and days > 0 else None
    )
    room = RoomFacts(
        sum_insured=terms.sum_insured,
        room_rate_per_day=room_rate,
        differential_billing=terms.differential_billing,
    )
    for row in payable():
        for rule_id in ("R13", "R14", "R15"):
            apply(row, rule_id, {"room": room, "package": package, "claim_route": route})

    # 4. The claim.
    ambulance = [r for r in payable() if r["head"] == Head.AMBULANCE.value]
    if ambulance:
        total = sum((r["admissible"] for r in ambulance), ZERO)
        spread("R19", ambulance, {"ambulance_total": total})
    r26: RuleResult | None = None
    for rule_id in ("R16", "R17", "R26"):
        result = spread(
            rule_id,
            payable(),
            {
                "sum_insured": terms.sum_insured,
                "admissible_total": admissible(),
                "procedure_codes": terms.procedure_codes,
                "diagnosis_codes": terms.diagnosis_codes,
                "preauth": preauth,
            },
        )
        r26 = result if rule_id == "R26" else r26
    [r21] = rules.evaluate(["R21"], Facts(admissible_total=admissible()), product)
    co_pay = money(-r21.amount_effect)
    for row in rows:
        row["deductions"] = [
            {"rule_id": r, "clause_ref": c, "amount": a} for a, r, c, _ in row["deductions"]
        ]
    cap = min(
        terms.sum_insured * product.room_rent.percent_of_sum_insured / 100,
        product.room_rent.max_per_day,
    )
    assert r26 is not None
    return {
        "claim_route": route,
        "room_rate_per_day": None if room_rate is None else money(room_rate),
        "room_cap_per_day": money(cap),
        "packages": [p.model_dump(mode="json") for p in terms.packages],
        "package_caps_money": bool(terms.packages) and bound,
        "lines": rows,
        "deductions": [row for row in rows if row["deducted"] > 0],
        "claimed_total": money(sum((ln.claimed for ln in lines), ZERO)),
        "admissible_total": admissible(),
        "co_pay": co_pay,
        "co_pay_rule": {"rule_id": r21.rule_id, "clause_ref": r21.clause_ref},
        "payable": money(admissible() - co_pay),
        "r20": None
        if r20 is None
        else {"passed": r20.passed, "amount_effect": money(r20.amount_effect)},
        "r26": {"passed": r26.passed, "amount_effect": money(r26.amount_effect)},
        "rules_failed": sorted({r.rule_id for r in results if not r.passed}),
    }


def _deduct(row: dict[str, Any], result: RuleResult, amount: Decimal) -> None:
    """Take amount off the line, never below zero; the line names the rule that took most."""
    take = money(min(max(amount, ZERO), row["admissible"]))
    if take == 0:
        return
    row["admissible"] = money(row["admissible"] - take)
    row["deducted"] = money(row["claimed"] - row["admissible"])
    row["deductions"].append((take, result.rule_id, result.clause_ref, result.reason))
    _, row["rule_id"], row["clause_ref"], row["reason"] = max(row["deductions"], key=lambda d: d[0])
