"""The corpus generator (tools/mockgen/corpus.py): a case spec in, a bundle and its ground
truth out. Deterministic by seed; every amount from the rules engine; each seeded problem
present in the documents as the ground truth says. No stack or model is needed."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest

from app.mock_core.seed import _POLICIES
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import DocType
from tools.mockgen import corpus
from tools.mockgen.corpus import CaseSpec, Problem, Variant
from tools.mockgen.sheet import SheetLine, expected_sheet

REPO = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 19)
D = Decimal

CLEAN = CaseSpec(
    case_id="t01-clean",
    category="clean_reimbursement",
    seed=11,
    route="reimbursement",
    policy=1,
    member=1,
    hospital=0,
    treatment="lap_appendicectomy",
    room="semi_private",
    problems=(),
    variant=Variant(delivery="merged"),
    expect={"final_status": "closed", "decision": "approve"},
)


def pdf_pages(path: Path) -> list[str]:
    doc = pdfium.PdfDocument(path.read_bytes())
    try:
        return [page.get_textpage().get_text_range() for page in doc]
    finally:
        doc.close()


def build(tmp: Path, spec: CaseSpec, **kwargs: Any) -> dict[str, Any]:
    return corpus.generate_case(spec, tmp / spec.case_id, today=TODAY, **kwargs)


def test_same_spec_and_day_give_identical_bytes_and_truth(tmp_path: Path) -> None:
    first = build(tmp_path / "a", CLEAN)
    second = build(tmp_path / "b", CLEAN)
    assert first == second
    names = sorted(p.name for p in (tmp_path / "a" / CLEAN.case_id).iterdir())
    for name in names:
        assert (tmp_path / "a" / CLEAN.case_id / name).read_bytes() == (
            tmp_path / "b" / CLEAN.case_id / name
        ).read_bytes(), name


def test_another_seed_changes_the_documents(tmp_path: Path) -> None:
    first = build(tmp_path / "a", CLEAN)
    other = build(tmp_path / "b", replace(CLEAN, seed=12))
    assert first["bill"]["bill_number"] != other["bill"]["bill_number"]


def test_the_ground_truth_file_is_what_generate_returns(tmp_path: Path) -> None:
    truth = build(tmp_path, CLEAN)
    written = json.loads((tmp_path / CLEAN.case_id / corpus.GROUND_TRUTH).read_text())
    assert written == truth


def test_the_payable_is_the_rules_engine_on_the_ground_truth_lines(tmp_path: Path) -> None:
    truth = build(tmp_path, CLEAN)
    rules = DefaultRulesEngine(REPO / "config" / "products")
    terms = corpus.sheet_terms(CLEAN)
    lines = [
        SheetLine(
            ln["line_id"],
            corpus.Head(ln["head"]),
            ln["description"],
            D(ln["qty"]),
            D(ln["rate"]),
            D(ln["amount"]),
            date.fromisoformat(ln["date"]),
        )
        for ln in truth["bill"]["lines"]
    ]
    again = expected_sheet(
        lines,
        rules,
        terms,
        admission=date.fromisoformat(truth["admission_date"]),
        discharge=date.fromisoformat(truth["discharge_date"]),
    )
    assert truth["sheet"]["payable"] == f"{again['payable']:.2f}"
    assert truth["expected"]["amount_paid"] == truth["sheet"]["payable"]
    assert truth["expected"]["icd_code"] == "K35.80"
    # A clean case: no line loses anything (co-pay alone does not make a partial).
    assert truth["sheet"]["deductions"] == []


def test_the_merged_pdf_has_one_label_per_page(tmp_path: Path) -> None:
    truth = build(tmp_path, CLEAN)
    merged = tmp_path / CLEAN.case_id / truth["files"]["merged"]
    assert len(pdf_pages(merged)) == len(truth["files"]["merged_pages"])
    assert set(truth["files"]["merged_pages"]) >= {
        DocType.CLAIM_FORM_PART_A.value,
        DocType.FINAL_BILL.value,
        DocType.PAYMENT_RECEIPT.value,
        DocType.CANCELLED_CHEQUE.value,
    }


def test_slots_write_one_file_per_document(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t02-slots", variant=Variant(delivery="slots"))
    truth = build(tmp_path, spec)
    assert truth["files"]["merged"] is None
    for item in truth["files"]["bundle"]:
        assert len(pdf_pages(tmp_path / spec.case_id / item["file"])) == item["pages"]


def test_the_persona_is_the_mock_core_member(tmp_path: Path) -> None:
    truth = build(tmp_path, replace(CLEAN, policy=3, member=1))
    n, si, *_, people = next(p for p in _POLICIES if p[0] == 3)
    assert truth["fields"]["identity.patient_name"] == people[0][0]
    assert truth["fields"]["identity.policy_number"] == f"MOCK/AS/2026/{100 + n:06d}"
    assert truth["persona"]["sum_insured"] == f"{D(si):.2f}"


def test_the_bill_and_the_patient_appear_in_the_pdf_text(tmp_path: Path) -> None:
    spec = replace(CLEAN, variant=Variant(delivery="slots"))
    truth = build(tmp_path, spec)
    bill = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "final_bill")
    text = " ".join(pdf_pages(tmp_path / spec.case_id / bill["file"]))
    assert truth["bill"]["bill_number"] in text
    assert truth["fields"]["identity.patient_name"].upper() in text


# --- Seeded problems ---------------------------------------------------------------------


def test_missing_document_is_left_out_and_written_as_the_query_reply(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t03", problems=(Problem.MISSING_DOCUMENT,))
    truth = build(tmp_path, spec)
    assert DocType.PAYMENT_RECEIPT.value not in truth["files"]["merged_pages"]
    assert truth["files"]["reply"]["doc_type"] == DocType.PAYMENT_RECEIPT.value
    assert (tmp_path / spec.case_id / truth["files"]["reply"]["file"]).exists()
    [problem] = truth["problems"]
    assert problem == {"type": "missing_document", "doc_type": "payment_receipt"}


def test_a_hidden_non_payable_item_is_deducted_by_r11(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t04", problems=(Problem.HIDDEN_NON_PAYABLE,))
    truth = build(tmp_path, spec)
    [problem] = truth["problems"]
    line = next(ln for ln in truth["sheet"]["lines"] if ln["line_id"] == problem["line_id"])
    assert line["rule_id"] == "R11"
    assert line["deducted"] == line["claimed"]


def test_bank_mismatch_puts_another_account_on_the_cheque(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t05", problems=(Problem.BANK_MISMATCH,))
    truth = build(tmp_path, spec)
    fields = truth["fields"]
    assert fields["bank.account_number"] != fields["claim_form_bank.account_number"]
    assert truth["form"]["bank_account_number"] == fields["claim_form_bank.account_number"]
    [problem] = truth["problems"]
    assert problem["type"] == "bank_mismatch"


def test_a_long_stay_exceeds_the_norm_by_at_least_two_days(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t06", problems=(Problem.LONG_STAY,))
    truth = build(tmp_path, spec)
    stay = (
        date.fromisoformat(truth["discharge_date"]) - date.fromisoformat(truth["admission_date"])
    ).days
    assert stay >= truth["expected"]["los_norm_days"] + 2


def test_an_inflated_bill_is_at_least_one_and_a_half_times_the_package(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t07", problems=(Problem.INFLATED_BILL,))
    truth = build(tmp_path, spec)
    rate = D(truth["expected"]["package_rate"])
    assert D(truth["sheet"]["claimed_total"]) >= rate * D("1.5")


def test_waiting_period_needs_the_policy_inside_its_initial_wait() -> None:
    with pytest.raises(ValueError, match="initial wait"):
        replace(CLEAN, problems=(Problem.WAITING_PERIOD,)).validate()
    replace(CLEAN, policy=6, problems=(Problem.WAITING_PERIOD,)).validate()


def test_an_undeclared_ped_is_in_the_history_and_not_on_the_policy(tmp_path: Path) -> None:
    spec = replace(CLEAN, case_id="t08", policy=2, problems=(Problem.UNDECLARED_PED,))
    truth = build(tmp_path, spec)
    [problem] = truth["problems"]
    assert problem["type"] == "undeclared_ped"
    assert problem["icd_family"] not in truth["persona"]["declared_ped_codes"]
    inception = date.fromisoformat(truth["persona"]["inception_date"])
    assert date.fromisoformat(problem["onset_before"]) < inception
    spec_slots = replace(spec, case_id="t08s", variant=Variant(delivery="slots"))
    truth = build(tmp_path, spec_slots)
    summary = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "discharge_summary")
    assert problem["history_text"] in " ".join(pdf_pages(tmp_path / "t08s" / summary["file"]))


def test_a_duplicate_file_reuses_the_bytes_of_another_case(tmp_path: Path) -> None:
    first = build(tmp_path, replace(CLEAN, variant=Variant(delivery="slots")))
    spec = replace(
        CLEAN,
        case_id="t09",
        seed=99,
        variant=Variant(delivery="slots"),
        problems=(Problem.DUPLICATE_FILE,),
        reuse_from=CLEAN.case_id,
    )
    truth = build(tmp_path, spec, corpus_dir=tmp_path)
    [problem] = truth["problems"]
    reused = next(f for f in truth["files"]["bundle"] if f["doc_type"] == problem["doc_type"])
    original = next(f for f in first["files"]["bundle"] if f["doc_type"] == problem["doc_type"])
    assert (tmp_path / "t09" / reused["file"]).read_bytes() == (
        tmp_path / CLEAN.case_id / original["file"]
    ).read_bytes()


def test_unbundling_is_only_for_cashless() -> None:
    with pytest.raises(ValueError, match="cashless"):
        replace(CLEAN, problems=(Problem.UNBUNDLING,)).validate()


# --- Variants ----------------------------------------------------------------------------


def test_a_phone_photo_page_has_no_text_layer(tmp_path: Path) -> None:
    spec = replace(
        CLEAN,
        case_id="t10",
        variant=Variant(delivery="slots", photo=frozenset({DocType.CANCELLED_CHEQUE})),
    )
    truth = build(tmp_path, spec)
    cheque = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "cancelled_cheque")
    assert cheque["page_variants"] == ["phone_photo"]
    assert pdf_pages(tmp_path / spec.case_id / cheque["file"]) == [""]


def test_a_stamp_and_a_handwritten_form_are_recorded(tmp_path: Path) -> None:
    spec = replace(
        CLEAN,
        case_id="t11",
        variant=Variant(
            delivery="slots", stamps=frozenset({DocType.FINAL_BILL}), handwritten_form=True
        ),
    )
    truth = build(tmp_path, spec)
    bill = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "final_bill")
    form = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "claim_form_part_a")
    assert "stamp" in bill["page_variants"][0]
    assert "handwritten" in form["page_variants"][0]
    assert "RECEIVED" in pdf_pages(tmp_path / spec.case_id / bill["file"])[0]


# --- Cashless ----------------------------------------------------------------------------


CASHLESS = replace(
    CLEAN,
    case_id="t20-cashless",
    category="cashless_full",
    route="cashless",
    policy=2,
    problems=(Problem.UNBUNDLING,),
    extra_days=1,
    expect={"final_status": "closed", "decision": "partial"},
)


def test_cashless_writes_three_requests_with_r23_and_r26_from_the_engine(tmp_path: Path) -> None:
    truth = build(tmp_path, CASHLESS)
    assert list(truth["requests"]) == ["preauth", "enhancement", "discharge"]
    discharge = truth["requests"]["discharge"]["sheet"]
    assert "R23" in discharge["rules_failed"]
    approved = D(truth["requests"]["enhancement"]["approved_total"])
    assert D(discharge["payable"]) <= approved
    for request, files in truth["files"]["requests"].items():
        for item in files:
            path = tmp_path / CASHLESS.case_id / item["file"]
            assert len(pdf_pages(path)) == item["pages"], (request, item)


def test_a_cashless_preauth_only_case_has_one_request(tmp_path: Path) -> None:
    spec = replace(
        CASHLESS,
        case_id="t21",
        policy=6,
        problems=(Problem.WAITING_PERIOD,),
        requests=("preauth",),
        extra_days=0,
        expect={"final_status": "closed", "decision": "reject"},
    )
    truth = build(tmp_path, spec)
    assert list(truth["requests"]) == ["preauth"]
    assert truth["expected"]["amount_paid"] == "0.00"
    admitted = date.fromisoformat(truth["admission_date"])
    assert TODAY - admitted < timedelta(days=18)


# --- The approved corpus (10 categories, one claim each) ---------------------------------


def test_the_corpus_has_the_ten_approved_categories_once_each() -> None:
    assert [s.category for s in corpus.CORPUS] == [
        "clean_reimbursement_paid",
        "room_breach_non_payable_partial_crc",
        "missing_document_query_reply_approve",
        "waiting_period_reject_crc",
        "duplicate_file_caught",
        "fraud_signals_investigator_referral",
        "bank_mismatch_operator_fix",
        "cashless_unbundling_hospital_paid",
        "cashless_preauth_denied",
        "query_unanswered_timeout_closure",
    ]
    assert len({s.case_id for s in corpus.CORPUS}) == 10


def test_a_lapsed_policy_case_needs_a_lapsed_policy() -> None:
    with pytest.raises(ValueError, match="grace period"):
        replace(CLEAN, problems=(Problem.LAPSED_POLICY,)).validate()


def test_the_corpus_avoids_risk_flags_it_does_not_seed(tmp_path: Path) -> None:
    """RF5 needs 3 other claims with one diagnosis at one hospital or by one doctor; RF6 an
    overlapping stay of one member; R25 a claim of Rs 1 lakh or more without KYC. Only the
    cases that seed a risk signal may carry one."""
    truths = corpus.generate_corpus(tmp_path, TODAY)
    per_hospital: dict[str, int] = {}
    stays: dict[str, list[tuple[date, date]]] = {}
    for truth in truths:
        per_hospital[truth["hospital"]["rohini"]] = (
            per_hospital.get(truth["hospital"]["rohini"], 0) + 1
        )
        member = truth["persona"]["member_id"]
        stay = (
            date.fromisoformat(truth["admission_date"]),
            date.fromisoformat(truth["discharge_date"]),
        )
        for other in stays.get(member, []):
            assert stay[1] < other[0] or other[1] < stay[0], member
        stays.setdefault(member, []).append(stay)
        assert D(truth["sheet"]["claimed_total"]) < D("100000"), truth["case_id"]
    assert max(per_hospital.values()) <= 3


def test_the_corpus_bundles_reach_their_seeded_problems(tmp_path: Path) -> None:
    truths = {t["category"]: t for t in corpus.generate_corpus(tmp_path, TODAY)}
    assert truths["clean_reimbursement_paid"]["sheet"]["deductions"] == []
    partial = truths["room_breach_non_payable_partial_crc"]["sheet"]
    assert {"R11", "R13", "R15"} <= set(partial["rules_failed"])
    fraud = truths["fraud_signals_investigator_referral"]
    assert {p["type"] for p in fraud["problems"]} == {"inflated_bill", "long_stay"}
    assert truths["cashless_preauth_denied"]["expected"]["amount_paid"] == "0.00"
    assert (
        truths["cashless_unbundling_hospital_paid"]["requests"]["discharge"]["sheet"][
            "rules_failed"
        ].count("R23")
        == 1
    )


def test_the_truth_holds_only_fields_the_documents_print(tmp_path: Path) -> None:
    """Part C prints age and sex, not the date of birth; a pre-auth alone has no discharge;
    a reused discharge summary carries the other case's patient and dates."""
    truths = {t["case_id"]: t for t in corpus.generate_corpus(tmp_path, TODAY)}
    cashless = truths["c08-cashless-unbundling"]["fields"]
    assert "identity.date_of_birth" not in cashless and "identity.gender" not in cashless
    assert "clinical.discharge_date" in cashless
    assert "clinical.discharge_date" not in truths["c09-preauth-denied"]["fields"]
    reused, source = truths["c05-duplicate-file"]["fields"], truths["c02-room-breach"]["fields"]
    for path in ("clinical.patient_name", "clinical.admission_date", "clinical.discharge_date"):
        assert reused[path] == source[path]


def test_stable_pdf_bytes_do_not_carry_the_save_time() -> None:
    """pdfium stamps a new document with the current time; two saves seconds apart must
    still give the same bytes."""
    one = b"<</CreationDate(D:20261119224131+05'30')/Creator(PDFium)>>"
    two = b"<</CreationDate(D:20261119230420+05'30')/Creator(PDFium)>>"
    assert corpus.stable(one) == corpus.stable(two)
    assert len(corpus.stable(one)) == len(one)


@pytest.mark.parametrize("seed", [101, 103, 106, 107])
def test_the_cheque_names_the_bank_of_its_ifsc(tmp_path: Path, seed: int) -> None:
    """Digitisation checks the IFSC's bank code against the bank printed on the cheque
    (ifsc_bank_code); a mock cheque must not trip it."""
    from app.services.digitisation._helpers import BANK_CODES

    spec = replace(CLEAN, case_id=f"bank{seed}", seed=seed, variant=Variant(delivery="slots"))
    truth = build(tmp_path, spec)
    cheque = next(f for f in truth["files"]["bundle"] if f["doc_type"] == "cancelled_cheque")
    text = " ".join(pdf_pages(tmp_path / spec.case_id / cheque["file"])).lower()
    code = truth["fields"]["bank.ifsc"][:4]
    names = BANK_CODES.get(code, ("bharat sahakari bank",))
    assert any(name in text for name in names), (code, text[:80])
