"""The generated tracer bundle (tools/mockgen/tracer_claim.py), intake to CLOSED.

The merged PDF goes through every real stage service, with a gateway scripted from
ground_truth.json: a perfect reader. So this checks the bundle and the workflow together:
the missing payment receipt in a merged upload raises a query, the reply answers it, and
the package, the payable, the per-line deductions and the payment equal the ground truth
that the generator computed with the rules engine.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.database.seed_icd_fixture import seed_icd_fixture
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.services.intake.contract import ClaimForm, SubmissionInput, UploadedFile
from app.shared.seed_ids import DOCTOR_USER_ID, FINANCE_USER_ID, MEMBER_USER_ID, OPS_USER_ID
from app.shared.types import DocType, RequestType, Stage
from app.workflows.claim_workflow import ClaimWorkflow
from app.workflows.worker import build_worker
from tests.knowledge import sources
from tests.services.reference_rows import add_reference_rows, add_tracer_wording_items
from tests.services.test_medical_review_coding import LAPAROSCOPIC_APPENDICECTOMY
from tests.services.test_registration import asha_policy
from tests.workflows.test_claim_end_to_end import TASK_QUEUE, Gateway, Harness
from tools.mockgen import tracer_claim as gen

ADMITTED = date(2026, 10, 29)  # inside the fake policy's period, two years after inception
CODE = "K35.80"


def v(value: Any, page: int = 1) -> dict[str, Any]:
    return {"value": value, "confidence": 0.97, "page": page, "handwritten": False}


def _script(truth: dict[str, Any]) -> dict[str, list[Any]]:
    """What a perfect reader would reply, from the ground truth."""
    f = truth["fields"]
    part_a = {
        "patient_name": v(f["identity.patient_name"]),
        "policy_number": v(f["identity.policy_number"]),
        "member_id": v(f["identity.member_id"]),
        "date_of_birth": v(f["identity.date_of_birth"]),
        "gender": v(f["identity.gender"]),
        "hospital_name": v(f["hospital.name"]),
        "hospital_city": v(f["hospital.city"]),
        "admission_date": v(f["admission.admission_date"]),
        "discharge_date": v(f["admission.discharge_date"]),
        "hospitalisation_due_to_injury": v(False),
        "claimed_hospital_expenses": v(f["claimed.hospital_expenses"], 2),
        "claimed_pre_hospitalisation": v(f["claimed.pre_hospitalisation"], 2),
        "claimed_post_hospitalisation": v(f["claimed.post_hospitalisation"], 2),
        "claimed_total": v(f["claimed.total"], 2),
        "bank_account_number": v(f["claim_form_bank.account_number"], 2),
        "ifsc": v(f["claim_form_bank.ifsc"], 2),
        "account_holder": v(f["claim_form_bank.account_holder"], 2),
    }
    clinical = truth["clinical"]
    summary = {
        "patient_name": v(f"Mrs. {gen.PATIENT.upper()}"),
        "admission_date": v(truth["admission_date"]),
        "discharge_date": v(truth["discharge_date"]),
        "diagnoses": [v(d) for d in clinical["diagnoses"]],
        "procedures": [v(p) for p in clinical["procedures"]],
        "icd_codes_written": [v(c) for c in clinical["hospital_codes"]],
        "facts": [
            {
                "kind": "history",
                "text": "Pain in the right lower abdomen",
                "duration": clinical["history_duration"],
                "page": 1,
                "confidence": 0.95,
            }
        ],
    }

    def page(section: str) -> int:
        return 1 if section in gen.PAGE_ONE_SECTIONS else 2

    bill = truth["bill"]
    final_bill = {
        "bill_number": v(bill["bill_number"]),
        "patient_name": v(gen.PATIENT),
        "lines": [
            {
                "section": ln["section"],
                "date": ln["date"],
                "description": ln["description"],
                "qty": ln["qty"],
                "rate": ln["rate"],
                "amount": ln["amount"],
                "page": page(ln["section"]),
                "confidence": 0.96,
                "handwritten": False,
            }
            for ln in bill["lines"]
        ],
        "section_totals": [
            {
                "section": s["section"],
                "amount": s["total"],
                "page": page(s["section"]),
                "confidence": 0.96,
            }
            for s in bill["section_totals"]
        ],
        "grand_total": v(bill["grand_total"], 2),
    }
    cheque = {
        "account_number": v(f["bank.account_number"]),
        "ifsc": v(f["bank.ifsc"]),
        "account_holder": v(f["bank.account_holder"]),
        "bank_name": v(gen.BANK),
    }
    labels = [{"doc_type": d, "confidence": 0.96} for d in truth["files"]["merged_pages"]]
    rewrite = {
        "items": [{"phrase": gen.DIAGNOSIS, "main_term": "appendicitis", "modifiers": ["acute"]}]
    }
    choose = {
        "codes": [
            {
                "code": CODE,
                "phrase": gen.DIAGNOSIS,
                "citations": [f"icd:{CODE}"],
                "rationale": "Acute appendicitis without perforation",
            }
        ],
    }
    query = {
        "decision": "query",
        "reasons": [],
        "summary": "The payment receipt is missing.",
        "query_items": ["payment_receipt"],
    }
    partial = {
        "decision": "partial",
        "reasons": [
            {
                "text": ln["reason"],
                "clause_ref": ln["clause_ref"],
                "rule_id": ln["rule_id"],
                "line_id": ln["line_id"],
            }
            for ln in truth["sheet"]["deductions"]
        ],
        "summary": "Room cap with proportionate deduction; non-payable items.",
    }
    package = truth["expected"]["package"]
    # R12: the one line the wording's lists leave unclear, read as the ground truth expects.
    [item] = gen.R12_READINGS.values()
    assert item is not None
    nursing = {"line_1": {"choice": f"List {item.list_name} item {item.item_number}: "
                                    f"{item.item}", "confidence": 0.95}}  # fmt: skip
    return {
        "classify_page": [*labels, {"doc_type": "payment_receipt", "confidence": 0.97}],
        "extract_claim_form_part_a": [part_a],
        "extract_discharge_summary": [summary],
        "extract_final_bill": [final_bill],
        "extract_cancelled_cheque": [cheque],
        # Coding, bill review and the full review run again after the reply.
        "medical_rewrite_terms": [rewrite, rewrite],
        "medical_pcs_step": [*LAPAROSCOPIC_APPENDICECTOMY] * 2,
        "medical_choose_codes": [choose, choose],
        "bill_non_payable": [nursing] * 2,
        "bill_package_choice": [{"procedure_1": package}] * 2,
        "medical_ped_reread": [{"statements": []}] * 2,
        "medical_findings": [{"findings": [], "query_items": []}] * 2,
        "adjudication_decide": [query, partial],
    }


@pytest.fixture
async def run(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[tuple[Harness, dict[str, Any], Path]]:
    truth = gen.generate(tmp_path, ADMITTED)
    await seed_claims_db(sessions)
    await seed_icd_fixture(sessions)
    await load_icd10pcs(sessions, sources.write_sources(tmp_path / "icd-2027"))
    await add_reference_rows(sessions)
    await add_tracer_wording_items(sessions)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        h = Harness(
            env,
            sessions,
            gateway=Gateway(_script(truth)),
            policy=asha_policy(sum_insured=gen.SUM_INSURED),
        )
        async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
            yield h, truth, tmp_path


async def test_the_generated_bundle_runs_from_a_merged_upload_to_closed(
    run: tuple[Harness, dict[str, Any], Path],
) -> None:
    h, truth, out = run
    form = ClaimForm.model_validate(truth["form"])
    submission = await h.intake.accept(
        SubmissionInput(
            request_type=RequestType.REIMBURSEMENT,
            submitted_by=MEMBER_USER_ID,
            channel="member",
            form=form,
            files=[UploadedFile(filename=gen.MERGED, data=(out / gen.MERGED).read_bytes())],
        )
    )
    claim_id = submission.claim_id
    assert submission.missing_documents == []  # merged: classification finds them

    await h.complete("confirm_registration", OPS_USER_ID, {"action": "confirm"})
    await h.complete("confirm_deductions", OPS_USER_ID, {"action": "confirm"})
    query = await h.wait_for(lambda: h.queries.open_query(claim_id), "the query")
    assert [(i.text, i.doc_type) for i in query.items] == [
        ("Payment receipt", DocType.PAYMENT_RECEIPT)
    ]
    await h.intake.accept_followup(
        claim_id,
        submitted_by=MEMBER_USER_ID,
        channel="member",
        files=[
            UploadedFile(
                filename=gen.REPLY,
                data=(out / gen.REPLY).read_bytes(),
                slot=DocType.PAYMENT_RECEIPT,
            )
        ],
    )
    await h.complete("confirm_deductions", OPS_USER_ID, {"action": "confirm"})  # after the reply
    await h.complete("approve", DOCTOR_USER_ID, {"action": "approve"})
    await h.complete("crc", DOCTOR_USER_ID, {"action": "uphold"})
    await h.complete("release_payment", FINANCE_USER_ID, {"action": "release"})
    result = await h.env.client.get_workflow_handle_for(ClaimWorkflow.run, str(claim_id)).result()

    records = StageRecordRepository(h.sessions)
    sheet = await records.latest(claim_id, Stage.BILL_REVIEW)
    assert sheet is not None
    expected = truth["sheet"]
    assert [p["package_code"] for p in sheet.payload["package"]["packages"]] == [
        truth["expected"]["package"]
    ]
    assert sheet.payload["payable"] == expected["payable"]
    assert [f["line_id"] for f in sheet.payload["findings"] if f["kind"] == "drug_price"] == [
        f["line_id"] for f in truth["drug_price_findings"]
    ]
    assert [
        (ln["line_id"], ln["head"], ln["rule_id"], ln["clause_ref"], ln["deducted"])
        for ln in sheet.payload["lines"]
    ] == [
        (ln["line_id"], ln["head"], ln["rule_id"], ln["clause_ref"], ln["deducted"])
        for ln in expected["lines"]
    ]
    medical = await records.latest(claim_id, Stage.MEDICAL_REVIEW)
    assert medical is not None
    assert [c["code"]["code"] for c in medical.payload["verified_codes"]] == [CODE]
    assert medical.payload["adverse"] is False  # the duration phrase is after inception
    guideline = medical.payload["guideline"]
    assert (guideline["guideline_code"], guideline["los_norm_days"]) == (
        truth["expected"]["guideline_code"],
        truth["expected"]["los_norm_days"],
    )

    letters = [r.payload["kind"] for r in await records.history(claim_id, Stage.COMMUNICATION)]
    assert letters == truth["expected"]["letters"]
    [(_, payment)] = h.money.payments.items()
    assert payment.amount == Decimal(truth["expected"]["amount_paid"])
    assert (result.status, result.draft) == ("closed", truth["expected"]["final_draft"])
