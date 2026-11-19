"""The generic expected sheet (tools/mockgen/sheet.py) runs the real rules engine in bill
review's order on any lines. It must reproduce the two hand-built acceptance answer keys
exactly, so the corpus generator inherits their correctness."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.rules.contract import Head, PreAuthFacts
from app.services.rules.service import DefaultRulesEngine
from tools.mockgen import cashless_claim as cashless
from tools.mockgen import tracer_claim as tracer
from tools.mockgen.sheet import SheetLine, SheetTerms, expected_sheet

REPO = Path(__file__).resolve().parents[2]
D = Decimal


@pytest.fixture(scope="module")
def rules() -> DefaultRulesEngine:
    return DefaultRulesEngine(REPO / "config" / "products")


def _without_in_package(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    """The hand-built keys predate the in_package flag the generic sheet records."""
    return [{k: v for k, v in line.items() if k != "in_package"} for line in lines]


def _tracer_lines(claim: tracer.TracerClaim) -> list[SheetLine]:
    return [
        SheetLine(n, i.head, i.description, i.qty, i.rate, i.amount, claim.on(i.day))
        for n, i in enumerate(claim.ordered_items, start=1)
    ]


def test_the_tracer_answer_key_is_reproduced(rules: DefaultRulesEngine) -> None:
    claim = tracer.TracerClaim(admission=date(2026, 10, 29))
    want = tracer.expected_sheet(claim, rules)
    got = expected_sheet(
        _tracer_lines(claim),
        rules,
        SheetTerms(
            route=tracer.CLAIM_ROUTE,
            sum_insured=tracer.SUM_INSURED,
            differential_billing=tracer.DIFFERENTIAL_BILLING,
            packages=[tracer.PACKAGE],
            non_package_discount=tracer.NON_PACKAGE_DISCOUNT,
            procedure_codes=[tracer.PCS_CODE],
            r12_readings=tracer.R12_READINGS,
        ),
        admission=claim.admission,
        discharge=claim.discharge,
    )
    assert got["payable"] == want["payable"] == D("60644.52")
    assert _without_in_package(got["lines"]) == want["lines"]
    assert got["co_pay"] == want["co_pay"]
    assert got["rules_failed"] == want["rules_failed"]


def test_the_cashless_discharge_answer_key_is_reproduced(rules: DefaultRulesEngine) -> None:
    claim = cashless.CashlessClaim(admission=date(2026, 11, 14))
    preauth = PreAuthFacts(approved_total=D("33000.00"), diagnosis_codes=["K35.80"])
    want = cashless.expected_sheet(
        cashless.bill_lines(claim),
        rules,
        admission=claim.admission,
        discharge=claim.discharge,
        preauth=preauth,
    )
    lines = [
        SheetLine(ln.line_id, ln.head, ln.description, ln.qty, ln.rate, ln.claimed, ln.service_date)
        for ln in cashless.bill_lines(claim)
    ]
    got = expected_sheet(
        lines,
        rules,
        SheetTerms(
            route="cashless",
            sum_insured=cashless.SUM_INSURED,
            differential_billing=cashless.DIFFERENTIAL_BILLING,
            packages=[cashless.PACKAGE],
            non_package_discount=cashless.NON_PACKAGE_DISCOUNT,
            procedure_codes=[cashless.PCS_CODE],
            diagnosis_codes=["K35.80"],
        ),
        admission=claim.admission,
        discharge=claim.discharge,
        preauth=preauth,
    )
    assert got["payable"] == want["payable"]
    assert _without_in_package(got["lines"]) == want["lines"]
    assert got["r26"] == want["r26"]
    assert got["rules_failed"] == want["rules_failed"]


def test_a_package_line_on_the_cashless_route_is_unbundled_by_r23(
    rules: DefaultRulesEngine,
) -> None:
    """A bill that charges the package as one line and also itemises a head the package
    covers: on a cashless claim the itemised line is deducted in full (R23)."""
    lines = [
        SheetLine(
            1,
            Head.PACKAGE,
            "Laparoscopic appendicectomy package",
            D("1"),
            D("33000"),
            D("33000.00"),
            date(2026, 11, 2),
        ),
        SheetLine(
            2,
            Head.CONSUMABLES,
            "Laparoscopic Trocar Kit",
            D("1"),
            D("4850"),
            D("4850.00"),
            date(2026, 11, 2),
        ),
    ]
    got = expected_sheet(
        lines,
        rules,
        SheetTerms(
            route="cashless",
            sum_insured=D("300000"),
            differential_billing=True,
            packages=[cashless.PACKAGE],
            non_package_discount=D("10"),
            procedure_codes=[cashless.PCS_CODE],
            billed_as_package=True,
        ),
        admission=date(2026, 11, 1),
        discharge=date(2026, 11, 4),
    )
    assert "R23" in got["rules_failed"]
    assert got["lines"][1]["deductions"] == [
        {"rule_id": "R23", "clause_ref": got["lines"][1]["clause_ref"], "amount": D("4850.00")}
    ]
    assert got["lines"][0]["deducted"] == D("0.00")
