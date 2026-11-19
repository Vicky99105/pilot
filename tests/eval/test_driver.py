"""How the eval driver acts a person at each checkpoint (tools/eval/run_eval.py `outcome`).
It accepts the system's drafts and fixes only what an operator would, so the eval measures
the system, not the driver."""

from typing import Any

from tools.eval.run_eval import outcome

TRUTH: dict[str, Any] = {
    "fields": {"bank.account_number": "11122233344", "identity.patient_name": "Asha Verma"},
    "form": {"bank_account_number": "11122233434"},
    "files": {
        "merged": "m.pdf",
        "merged_pages": ["claim_form_part_a", "final_bill"],
        "bundle": [],
        "reply": {"doc_type": "payment_receipt"},
    },
    "expected": {"decision": "approve"},
}


def task(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kind": kind, "payload": payload or {}}


def test_labels_are_fixed_to_the_ground_truth_by_page_order() -> None:
    pages = [
        {"page_id": "b", "seq": 2, "doc_type": "other"},
        {"page_id": "a", "seq": 1, "doc_type": "claim_form_part_a"},
    ]
    assert outcome(task("fix_labels", {"pages": pages}), TRUTH) == {
        "action": "confirm",
        "labels": {"b": "final_bill"},
    }


def test_fields_are_fixed_to_the_ground_truth_and_the_bank_only_on_request() -> None:
    payload = {
        "fields": {"identity.patient_name": {"value": "Asha Varma"}},
        "failed_checks": [{"check_id": "bank_matches_cheque"}],
    }
    assert outcome(task("fix_fields", payload), TRUTH) == {
        "action": "confirm",
        "fields": {"identity.patient_name": "Asha Verma"},
    }
    fixing = TRUTH | {"expected": {"fix_bank": True}}
    fixed = outcome(task("fix_fields", payload), fixing)
    assert fixed["fields"]["bank.account_number"] == "11122233434"


def test_the_approver_accepts_the_draft_unless_a_query_closed_without_reply() -> None:
    assert outcome(task("approve", {"draft": {"decision": "partial"}}), TRUTH) == {
        "action": "approve"
    }
    closed = outcome(task("approve", {"draft": {"decision": "query"}}), TRUTH)
    assert (closed["action"], closed["decision"]) == ("override", "reject")
    assert closed["reason"]


def test_the_query_timeout_and_the_investigator_follow_the_case() -> None:
    assert outcome(task("query_no_reply"), TRUTH | {"expected": {"query_reply": False}}) == {
        "action": "proceed"
    }
    assert outcome(task("query_no_reply"), TRUTH)["action"] == "extend"
    verdict = outcome(
        task("investigate"), TRUTH | {"expected": {"investigator": "fraud_confirmed"}}
    )
    assert verdict["action"] == "fraud_confirmed" and verdict["note"]
    assert outcome(task("investigate"), TRUTH)["action"] == "cleared"
