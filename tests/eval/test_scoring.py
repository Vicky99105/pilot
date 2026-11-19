"""Eval scoring (tools/eval/scoring.py): pure functions from a ground truth and what the
system recorded to metrics. No stack and no model."""

from typing import Any

import pytest

from tools.eval import scoring


def test_page_labels_compare_in_page_order() -> None:
    got = [
        {"seq": 2, "doc_type": "final_bill"},
        {"seq": 1, "doc_type": "claim_form_part_a"},
        {"seq": 3, "doc_type": "other"},
    ]
    result = scoring.page_labels(["claim_form_part_a", "final_bill", "cancelled_cheque"], got)
    assert (result["correct"], result["total"]) == (2, 3)
    assert result["confusions"] == [("cancelled_cheque", "other")]


def test_fields_count_exact_and_normalised_matches() -> None:
    truth = {
        "identity.patient_name": "Asha Verma",
        "bill.grand_total": "44486.00",
        "admission.admission_date": "2026-10-05",
        "bank.ifsc": "PUNB0468286",
    }
    record = {
        "identity": {"patient_name": {"value": "Mrs. ASHA VERMA"}},
        "bill": {"grand_total": {"value": "44,486.00"}},
        "admission": {"admission_date": {"value": "2026-10-05"}},
        "bank": {"ifsc": {"value": "PUNB0468268"}},
    }
    result = scoring.fields(truth, record)
    assert (result["exact"], result["normalised"], result["total"]) == (1, 3, 4)
    assert [f["field"] for f in result["wrong"]] == ["bank.ifsc"]


def test_bill_lines_match_on_amount_and_description_once_each() -> None:
    want = [
        {"description": "Surgeon Fee", "amount": "10000.00"},
        {"description": "Consultant Visit", "amount": "800.00"},
        {"description": "Consultant Visit", "amount": "800.00"},
    ]
    got = [
        {"description": {"value": "SURGEON FEE"}, "amount": {"value": "10,000.00"}},
        {"description": {"value": "Consultant Visit"}, "amount": {"value": "800"}},
        {"description": {"value": "Nursing"}, "amount": {"value": "800"}},
    ]
    result = scoring.bill_lines(want, got)
    assert (result["matched"], result["expected"], result["extracted"]) == (2, 3, 3)


def _line(description: str, *rules: str) -> dict[str, Any]:
    return {
        "description": description,
        "deductions": [{"rule_id": r, "amount": "1"} for r in rules],
    }


def test_deductions_give_precision_and_recall_per_rule() -> None:
    want = [_line("Room", "R13"), _line("Gloves", "R11"), _line("Nursing", "R12"), _line("OT")]
    got = [_line("Room", "R13"), _line("Gloves"), _line("Nursing", "R12"), _line("OT", "R15")]
    result = scoring.deductions(want, got)
    assert result["per_rule"]["R13"] == {"tp": 1, "fp": 0, "fn": 0}
    assert result["per_rule"]["R11"] == {"tp": 0, "fp": 0, "fn": 1}
    assert result["per_rule"]["R15"] == {"tp": 0, "fp": 1, "fn": 0}
    assert (result["tp"], result["fp"], result["fn"]) == (2, 1, 1)


def test_codes_score_exact_three_character_and_pcs_per_character() -> None:
    assert scoring.icd("K35.80", ["K35.89", "R10.31"]) == {"exact": False, "three_char": True}
    assert scoring.icd("K35.80", ["K35.80"]) == {"exact": True, "three_char": True}
    assert scoring.icd("K35.80", []) == {"exact": False, "three_char": False}
    assert scoring.pcs_per_char("0DTJ4ZZ", ["0DTJ0ZZ"]) == pytest.approx(6 / 7)
    assert scoring.pcs_per_char("0DTJ4ZZ", []) == 0.0
    assert scoring.pcs_per_char(None, ["0DTJ4ZZ"]) is None


def _observed(**parts: Any) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "intake": {},
        "classification": {},
        "digitisation": {},
        "registration": {},
        "bill_review": {"lines": []},
        "medical_review": {},
        "risk": {},
        "queries": [],
        "events": [],
        "submission": {},
    }
    return empty | parts


@pytest.mark.parametrize(
    ("problem", "observed", "found"),
    [
        (
            {"type": "missing_document", "doc_type": "payment_receipt"},
            _observed(
                classification={"mismatches": [{"kind": "missing", "doc_type": "payment_receipt"}]}
            ),
            True,
        ),
        ({"type": "missing_document", "doc_type": "payment_receipt"}, _observed(), False),
        (
            {"type": "hidden_non_payable", "description": "Mask"},
            _observed(bill_review={"lines": [_line("MASK", "R11")]}),
            True,
        ),
        (
            {"type": "unbundling", "description": "Trocar Kit"},
            _observed(bill_review={"lines": [_line("Trocar kit", "R20")]}),
            False,
        ),
        (
            {"type": "waiting_period"},
            _observed(registration={"rule_results": [{"rule_id": "R5", "passed": False}]}),
            True,
        ),
        ({"type": "long_stay"}, _observed(risk={"hits": [{"rule_id": "RF12"}]}), True),
        (
            {"type": "long_stay"},
            _observed(medical_review={"findings": [{"kind": "length_of_stay"}]}),
            True,
        ),
        (
            {"type": "undeclared_ped"},
            _observed(medical_review={"findings": [{"kind": "pre_existing"}]}),
            True,
        ),
        ({"type": "duplicate_file"}, _observed(risk={"hits": [{"rule_id": "RF3"}]}), True),
        ({"type": "duplicate_file"}, _observed(submission={"duplicate_of": "x"}), True),
        (
            {"type": "bank_mismatch"},
            _observed(digitisation={"failed_checks": [{"check_id": "bank_matches_cheque"}]}),
            True,
        ),
        ({"type": "inflated_bill"}, _observed(risk={"hits": [{"rule_id": "RF11"}]}), True),
    ],
)
def test_each_seeded_problem_has_a_detector(
    problem: dict[str, Any], observed: dict[str, Any], found: bool
) -> None:
    assert scoring.detected(problem, observed) is found


def test_every_problem_type_of_the_generator_has_a_detector() -> None:
    from tools.mockgen.corpus import Problem

    assert {p.value for p in Problem} <= set(scoring.DETECTORS)


def test_model_calls_give_cost_latency_and_models_per_role() -> None:
    calls = [
        {"role": "reasoning", "model": "m1", "cost": "0.002", "latency_ms": 900, "valid": True},
        {"role": "vision_extract", "model": "m2", "cost": None, "latency_ms": 1100, "valid": False},
    ]
    result = scoring.model_calls(calls)
    assert result["cost_usd"] == pytest.approx(0.002)
    assert result["latency_ms"] == 2000
    assert result["invalid"] == 1
    assert result["models"] == {"reasoning": ["m1"], "vision_extract": ["m2"]}
    assert result["uncosted_calls"] == 1


def test_the_summary_aggregates_cases() -> None:
    cases = [
        {
            "case_id": "a",
            "page_labels": {"correct": 9, "total": 10},
            "fields": {"exact": 5, "normalised": 8, "total": 10},
            "bill_lines": {"matched": 18, "expected": 20},
            "deductions": {
                "tp": 2,
                "fp": 0,
                "fn": 1,
                "per_rule": {"R11": {"tp": 1, "fp": 0, "fn": 1}},
            },
            "payable": {"exact": True},
            "icd": {"exact": True, "three_char": True},
            "pcs_per_char": 1.0,
            "package": {"match": True},
            "problems": [{"type": "bank_mismatch", "detected": True}],
            "queried": True,
            "model_calls": {"cost_usd": 0.03, "latency_ms": 60000},
            "wall_seconds": 120.0,
        },
        {
            "case_id": "b",
            "page_labels": {"correct": 10, "total": 10},
            "fields": {"exact": 10, "normalised": 10, "total": 10},
            "bill_lines": {"matched": 20, "expected": 20},
            "deductions": {
                "tp": 1,
                "fp": 1,
                "fn": 0,
                "per_rule": {"R11": {"tp": 1, "fp": 1, "fn": 0}},
            },
            "payable": {"exact": False},
            "icd": {"exact": False, "three_char": True},
            "pcs_per_char": 0.5,
            "package": {"match": None},
            "problems": [{"type": "bank_mismatch", "detected": False}],
            "queried": False,
            "model_calls": {"cost_usd": 0.01, "latency_ms": 20000},
            "wall_seconds": 60.0,
        },
    ]
    summary = scoring.summarise(cases)
    assert summary["page_label_accuracy"] == pytest.approx(19 / 20)
    assert summary["field_accuracy_normalised"] == pytest.approx(18 / 20)
    assert summary["bill_line_match"] == pytest.approx(38 / 40)
    assert summary["deduction_precision"] == pytest.approx(3 / 4)
    assert summary["deduction_recall"] == pytest.approx(3 / 4)
    assert summary["per_rule"]["R11"] == {"precision": 2 / 3, "recall": 2 / 3}
    assert summary["payable_exact"] == pytest.approx(0.5)
    assert summary["icd_exact"] == pytest.approx(0.5)
    assert summary["icd_three_char"] == 1.0
    assert summary["pcs_per_char"] == pytest.approx(0.75)
    assert summary["package_match"] == 1.0  # cases with no expected package are left out
    assert summary["detection"]["bank_mismatch"] == {"detected": 1, "seeded": 2}
    assert summary["query_rate"] == pytest.approx(0.5)
    assert summary["cost_per_claim_usd"] == pytest.approx(0.02)
    assert summary["wall_seconds_per_claim"] == pytest.approx(90.0)
    assert "| Payable exact | 50.0% |" in scoring.markdown(summary, cases)
