"""A cashless admission, pre-auth to CLOSED, through every real stage service.

Time-skipping Temporal, SQLite, a scripted model gateway, and fake core money. Sunrise
(CGHS NABH, X tier) asks a pre-auth of Rs 28,000 for a laparoscopic appendicectomy: below
the AG095 package rate of Rs 33,000, so it is approved less the 5% co-pay. An enhancement
adds Rs 7,000 for an extra day; the revised estimate (Rs 35,000) is capped at the package
(the route is cashless), so the total rises to Rs 33,000 before co-pay and the block rises
with it. The final bill adds an ambulance outside the package: R26 holds the admissible
total to the Rs 33,000 approved, and the hospital is paid Rs 31,350.
"""

import json
import re
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.database.models import ClaimRow
from app.database.seed import seed_claims_db
from app.database.seed_icd_fixture import seed_icd_fixture
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.model_gateway.contract import Message, ModelRole, T
from app.shared.seed_ids import (
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    HOSPITAL_ROHINI_IDS,
    HOSPITAL_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    OPS_USER_ID,
)
from app.shared.types import DocType, RequestType, Stage
from app.workflows.claim_workflow import ClaimWorkflow
from app.workflows.worker import build_worker
from tests.docs import make_pdf
from tests.knowledge import sources
from tests.services.claim_builders import DIAGNOSIS
from tests.services.reference_rows import add_reference_rows
from tests.services.test_digitisation import line, section, v
from tests.services.test_medical_review_coding import LAPAROSCOPIC_APPENDICECTOMY
from tests.workflows.test_claim_end_to_end import CHOOSE, REWRITE, TASK_QUEUE, Gateway, Harness

D = Decimal
ROHINI = HOSPITAL_ROHINI_IDS[0]
PROCEDURE = "Laparoscopic appendicectomy"


def head(name: str, amount: str, days: str | None = None) -> dict[str, Any]:
    return {"head": name, "days": days, "amount": amount, "page": 1, "confidence": 0.97}


PART_C: dict[str, Any] = {
    "patient_name": v("Asha Verma"),
    "policy_number": v(MEMBER_POLICY_NUMBER),
    "member_id": v(MEMBER_MEMBER_ID),
    "date_of_birth": v("1984-06-12"),
    "gender": v("F"),
    "hospital_name": v("Sunrise Multispeciality Hospital"),
    "hospital_city": v("New Delhi"),
    "rohini_id": v(ROHINI),
    "treating_doctor": v("Dr. Rajiv Khanna"),
    "doctor_registration_number": v("DMC/R/04512"),
    "presenting_complaints": v("Right iliac fossa pain since 2 days with fever"),
    "provisional_diagnosis": v(DIAGNOSIS),
    "icd_code_written": v("K35.8"),
    "proposed_treatment": v(PROCEDURE),
    "pcs_code_written": v(None),
    "admission_date": v("2026-11-02"),
    "admission_type": v("emergency"),
    "expected_stay_days": v(3),
    "room_type": v("Semi-private"),
    "hospitalisation_due_to_injury": v(False),
    "estimate": [
        head("Room rent", "12000", days="3"),
        head("OT charges", "8000"),
        head("Surgeon fees", "5000"),
        head("Medicines", "3000"),
    ],
    "estimate_total": v("28000"),
}
ENHANCEMENT: dict[str, Any] = {
    "extra_amount": v("7000"),
    "reason": v("Post-operative ileus; one more day of stay"),
    "extra_days": v(1),
    "revised_discharge_date": v("2026-11-06"),
    "estimate": [head("Room rent", "4000", days="1"), head("Medicines", "3000")],
}
INTERIM: dict[str, Any] = {
    "bill_number": v("SMH/INT/77"),
    "patient_name": v("Asha Verma"),
    "lines": [line("Room Charges", "Semi-private room", "3", "4000", "12000")],
    "section_totals": [],
    "grand_total": v("26500"),
}
SUMMARY: dict[str, Any] = {
    "patient_name": v("Asha Verma"),
    "admission_date": v("2026-11-02"),
    "discharge_date": v("2026-11-06"),
    "diagnoses": [v(DIAGNOSIS)],
    "procedures": [v(PROCEDURE)],
    "icd_codes_written": [v("K35.8")],
    "facts": [{"kind": "finding", "text": "USG: inflamed appendix", "page": 1,
               "confidence": 0.95}],
}  # fmt: skip
# Rs 44,250: the package heads (Rs 42,250) and an ambulance outside the package.
FINAL_BILL: dict[str, Any] = {
    "bill_number": v("SMH/IP/2026/2001"),
    "patient_name": v("Asha Verma"),
    "lines": [
        line("Room Charges", "Semi-private room", "4", "4500", "18000"),
        line("Professional Fees", "Surgeon visits", "4", "1500", "6000"),
        line("Pharmacy", "Drugs and IV fluids", "1", "8250", "8250"),
        line("Investigations", "USG and blood tests", "1", "10000", "10000"),
        line("Ambulance", "Ambulance to hospital", "1", "2000", "2000"),
    ],
    "section_totals": [
        section("Room Charges", "18000"),
        section("Professional Fees", "6000"),
        section("Pharmacy", "8250"),
        section("Investigations", "10000"),
        section("Ambulance", "2000"),
    ],
    "grand_total": v("44250"),
}
PREAUTH_FILES = [(DocType.PREAUTH_FORM, "PART C"), (DocType.INVESTIGATION_REPORT, "USG")]
ENHANCEMENT_FILES = [(DocType.ENHANCEMENT_REQUEST, "ENHANCE"), (DocType.INTERIM_BILL, "INTERIM")]
DISCHARGE_FILES = [
    (DocType.DISCHARGE_SUMMARY, "SUMMARY"),
    (DocType.FINAL_BILL, "BILL"),
    (DocType.OT_NOTES, "OT NOTES"),
]


class CashlessGateway(Gateway):
    """The script, plus two replies a model writes from what the call shows it: R12 reads
    every unclear line as payable, and the decision's reasons explain each deduction."""

    async def structured(
        self,
        role: ModelRole,
        messages: list[Message],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        if purpose == "bill_non_payable":
            reading = {"choice": "payable", "confidence": 1.0}
            self._script[purpose] = [{name: reading for name in schema.model_fields}]
        if purpose == "adjudication_decide":
            self._script[purpose] = [_reasons(messages[1].content)]
        return await super().structured(role, messages, schema, claim_id=claim_id, purpose=purpose)


def _reasons(content: str) -> dict[str, Any]:
    inputs = json.loads(re.search(r"Inputs:\n(.*)\n\nClauses:", content, re.S).group(1))  # type: ignore[union-attr]
    deductions = inputs["deductions"]
    reasons = [
        {"text": d["reason"] or "deducted", "clause_ref": d["clause_ref"],
         "rule_id": d["rule_id"], "line_id": d["line_id"]}
        for d in deductions
    ]  # fmt: skip
    if not reasons:
        reasons = [{"text": "Within the policy terms", "rule_id": "R21"}]
    decision = "partial" if deductions else "approve"
    return {"decision": decision, "reasons": reasons, "summary": f"{decision} on the sheet"}


def _gateway() -> CashlessGateway:
    pages = [*PREAUTH_FILES, *ENHANCEMENT_FILES, *DISCHARGE_FILES]
    return CashlessGateway(
        {
            "classify_page": [{"doc_type": d.value, "confidence": 0.97} for d, _ in pages],
            "extract_preauth_form": [PART_C],
            "extract_enhancement_request": [ENHANCEMENT],
            "extract_interim_bill": [INTERIM],
            "extract_discharge_summary": [SUMMARY],
            "extract_final_bill": [FINAL_BILL],
            "medical_rewrite_terms": [REWRITE] * 3,
            "medical_pcs_step": [*LAPAROSCOPIC_APPENDICECTOMY] * 3,
            "medical_choose_codes": [CHOOSE] * 3,
            "bill_package_choice": [{"procedure_1": "AG095"}] * 3,
            "medical_findings": [{"findings": [], "query_items": []}] * 3,
        }
    )


class CashlessHarness(Harness):
    async def preauth(self) -> Submission:
        return await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.PREAUTH,
                submitted_by=HOSPITAL_USER_ID,
                channel="hospital",
                form=ClaimForm(
                    policy_number=MEMBER_POLICY_NUMBER,
                    member_id=MEMBER_MEMBER_ID,
                    patient_name="Asha Verma",
                    hospital_id=ROHINI,
                    admission_date=date(2026, 11, 2),
                    claimed_amount=D("28000"),
                ),
                files=_files(PREAUTH_FILES),
            )
        )

    async def request(
        self, claim_id: UUID, request_type: RequestType, files: list[tuple[DocType, str]]
    ) -> Submission:
        return await self.intake.accept_followup(
            claim_id,
            submitted_by=HOSPITAL_USER_ID,
            channel="hospital",
            files=_files(files),
            request_type=request_type,
        )

    async def review(self, *, crc: bool) -> None:
        await self.complete("confirm_registration", OPS_USER_ID, {"action": "confirm"})
        await self.complete("confirm_deductions", OPS_USER_ID, {"action": "confirm"})
        await self.complete("approve", DOCTOR_USER_ID, {"action": "approve"})
        if crc:
            await self.complete("crc", DOCTOR_USER_ID, {"action": "uphold"})

    async def status(self, claim_id: UUID) -> ClaimRow:
        async with self.sessions() as session:
            row = await session.get(ClaimRow, claim_id)
        assert row is not None
        return row


def _files(files: list[tuple[DocType, str]]) -> list[UploadedFile]:
    return [UploadedFile(filename=f"{d.value}.pdf", data=make_pdf([t]), slot=d) for d, t in files]


@pytest.fixture
async def harness(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[CashlessHarness]:
    await seed_claims_db(sessions)
    await seed_icd_fixture(sessions)
    await load_icd10pcs(sessions, sources.write_sources(tmp_path / "icd-2027"))
    await add_reference_rows(sessions)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        h = CashlessHarness(env, sessions, gateway=_gateway())
        async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
            yield h


async def test_preauth_enhancement_and_discharge_pay_the_hospital(
    harness: CashlessHarness,
) -> None:
    h = harness
    claim_id = (await h.preauth()).claim_id

    # Pre-auth: Rs 28,000 is below the package rate; approved less the 5% co-pay.
    await h.review(crc=False)
    await h.wait_for(lambda: _status_is(h, claim_id, "awaiting_discharge"), "awaiting discharge")
    first = await h.records.latest(claim_id, Stage.AUTHORISATION)
    assert first is not None
    assert (first.payload["approved_total"], first.payload["blocked"]) == ("26600.00", "26600.00")

    # Enhancement: the revised estimate is capped at AG095, so the total rises to 33,000.
    await h.request(claim_id, RequestType.ENHANCEMENT, ENHANCEMENT_FILES)
    await h.review(crc=True)
    await h.wait_for(lambda: _authorisations(h, claim_id, 2), "the enhancement's authorisation")
    raised = await h.records.latest(claim_id, Stage.AUTHORISATION)
    assert raised is not None
    assert (raised.payload["approved_now"], raised.payload["approved_total"]) == (
        "4750.00", "31350.00")  # fmt: skip
    assert raised.payload["approved_admissible"] == "33000.00"
    assert [a.amount for a in h.money.adjustments.values()] == [D("26600.00"), D("31350.00")]
    await h.wait_for(lambda: _status_is(h, claim_id, "awaiting_discharge"), "awaiting again")

    # Discharge: R26 holds the admissible total to the 33,000 approved; the hospital is paid.
    await h.request(claim_id, RequestType.DISCHARGE, DISCHARGE_FILES)
    await h.review(crc=True)
    await h.complete("release_payment", FINANCE_USER_ID, {"action": "release"})
    result = await h.env.client.get_workflow_handle_for(ClaimWorkflow.run, str(claim_id)).result()

    sheet = await h.records.latest(claim_id, Stage.BILL_REVIEW)
    assert sheet is not None
    [r26] = [r for r in sheet.payload["rule_results"] if r["rule_id"] == "R26"]
    assert (r26["passed"], r26["amount_effect"]) == (False, "-1800.00")
    assert (sheet.payload["admissible_total"], sheet.payload["payable"]) == (
        "33000.00", "31350.00")  # fmt: skip
    [payment] = h.money.payments.values()
    assert payment.amount == D("31350.00")
    settled = await h.records.latest(claim_id, Stage.SETTLEMENT)
    assert settled is not None
    assert settled.payload["payee"]["kind"] == "hospital"
    assert settled.payload["payee"]["name"] == "Sunrise Multispeciality Hospital"
    [(_, utilised)] = h.money.releases.values()
    assert utilised == D("31350.00")
    assert result.status == "closed"
    letters = [r.payload for r in await h.records.history(claim_id, Stage.COMMUNICATION)]
    assert [(ltr["kind"], ltr["request_type"]) for ltr in letters] == [
        ("authorisation", "preauth"),
        ("authorisation", "enhancement"),
        ("authorisation", "discharge"),
    ]
    row = await h.status(claim_id)
    assert row.status == "closed" and row.originals_due_at is not None
    assert row.originals_received_at is None  # the operator marks them on arrival
    events = await h.monitoring.events(claim_id)
    assert [e.detail["request_type"] for e in events if e.action == "request_started"] == [  # type: ignore[index]
        "preauth", "enhancement", "discharge"]  # fmt: skip
    assert not [e for e in events if e.action == "clock_breached"]


async def _status_is(h: CashlessHarness, claim_id: UUID, status: str) -> bool:
    return (await h.status(claim_id)).status == status


async def _authorisations(h: CashlessHarness, claim_id: UUID, n: int) -> bool:
    return len(await h.records.history(claim_id, Stage.AUTHORISATION)) >= n
