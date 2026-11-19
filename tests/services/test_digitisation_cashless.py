"""Digitisation of cashless requests: the pre-auth form (Part C), an enhancement with its
interim bill, and the discharge bundle merged over the record. Checks run in code."""

import copy
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.services.classification.service import DefaultClassificationService
from app.services.digitisation.contract import ClaimRecord
from app.services.digitisation.service import DefaultDigitisationService
from app.services.intake.contract import ClaimForm, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.page_prep.service import DefaultPagePreparer
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS, HOSPITAL_USER_ID
from app.shared.types import Code, DocType, RequestType
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.test_digitisation import BILL, DISCHARGE, line, section, v

POLICY = "MOCK/AS/2026/000102"
MEMBER = "MOCK-M-000102-01"
ROHINI = HOSPITAL_ROHINI_IDS[0]
ADMITTED = date(2026, 11, 2)


def head(name: str, amount: str, days: str | None = None) -> dict[str, Any]:
    return {"head": name, "days": days, "amount": amount, "page": 1, "confidence": 0.96}


PART_C: dict[str, Any] = {
    "patient_name": v("Imran Qureshi"),
    "policy_number": v(POLICY),
    "member_id": v(MEMBER),
    "date_of_birth": v("1990-09-30"),
    "gender": v("M"),
    "hospital_name": v("Sunrise Multispeciality Hospital"),
    "hospital_city": v("New Delhi"),
    "rohini_id": v(ROHINI),
    "treating_doctor": v("Dr. Rajiv Khanna"),
    "doctor_registration_number": v("DMC/R/04512"),
    "presenting_complaints": v("Pain in right lower abdomen since 2 days, fever"),
    "provisional_diagnosis": v("Acute appendicitis"),
    "icd_code_written": v("K35.8"),
    "proposed_treatment": v("Laparoscopic appendicectomy"),
    "pcs_code_written": v(None),
    "admission_date": v(ADMITTED.isoformat()),
    "admission_type": v("emergency"),
    "expected_stay_days": v(3),
    "room_type": v("Semi-private"),
    "hospitalisation_due_to_injury": v(False),
    "estimate": [
        head("Room rent", "9000", days="3"),
        head("OT charges", "8000"),
        head("Surgeon and anaesthetist fees", "9000"),
        head("Medicines and consumables", "4000"),
    ],
    "estimate_total": v("30000"),
}
ENHANCEMENT: dict[str, Any] = {
    "extra_amount": v("9000"),
    "reason": v("Post-operative ileus; one more day of stay"),
    "extra_days": v(1),
    "revised_discharge_date": v("2026-11-06"),
    "estimate": [head("Room rent", "3000", days="1"), head("Medicines", "6000")],
}
INTERIM: dict[str, Any] = {
    "bill_number": v("SMH/INT/1"),
    "patient_name": v("Imran Qureshi"),
    "lines": [line("Room Charges", "Semi-private room", "3", "3000", "9000")],
    "section_totals": [],
    "grand_total": v("29500"),
}


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.intake = DefaultIntakeService(sessions, self.files, FakeWorkflowStarter())
        self.part_c = copy.deepcopy(PART_C)

    def form(self, hospital_id: str = ROHINI) -> ClaimForm:
        return ClaimForm(
            policy_number=POLICY,
            member_id=MEMBER,
            patient_name="Imran Qureshi",
            hospital_id=hospital_id,
            admission_date=ADMITTED,
            claimed_amount=Decimal("30000"),
        )

    async def submit(
        self,
        request_type: RequestType,
        pages: list[tuple[DocType, str]],
        replies: dict[str, Any],
        *,
        parent: UUID | None = None,
        hospital_id: str = ROHINI,
    ) -> UUID:
        submission = await self.intake.accept(
            SubmissionInput(
                request_type=request_type,
                submitted_by=HOSPITAL_USER_ID,
                channel="hospital",
                form=self.form(hospital_id),
                files=[UploadedFile(filename="r.pdf", data=make_pdf([t for _, t in pages]))],
                parent_claim_id=parent,
            )
        )
        gateway = FakeModelGateway(
            {
                "classify_page": [{"doc_type": t.value, "confidence": 0.95} for t, _ in pages],
                **{purpose: [reply] for purpose, reply in replies.items()},
            }
        )
        classification = DefaultClassificationService(
            self.sessions, self.files, gateway, DefaultPagePreparer(self.sessions, self.files)
        )
        await classification.classify(submission.submission_id)
        self.gateway = gateway
        return submission.claim_id

    async def extract(self, claim_id: UUID) -> ClaimRecord:
        return await DefaultDigitisationService(self.sessions, self.files, self.gateway).extract(
            claim_id
        )

    async def preauth(self, **kwargs: Any) -> tuple[UUID, ClaimRecord]:
        claim_id = await self.submit(
            RequestType.PREAUTH,
            [(DocType.PREAUTH_FORM, "PART C")],
            {"extract_preauth_form": self.part_c},
            **kwargs,
        )
        return claim_id, await self.extract(claim_id)


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


def failed(record: ClaimRecord) -> dict[str, list[str]]:
    return {c.check_id: c.fields for c in record.failed_checks}


async def test_part_c_fills_the_preauth_section_and_the_shared_sections(env: Env) -> None:
    _, record = await env.preauth()

    pre = record.preauth
    assert pre.rohini_id.value == ROHINI
    assert pre.treating_doctor.value == "Dr. Rajiv Khanna"
    assert pre.doctor_registration_number.value == "DMC/R/04512"
    assert pre.expected_stay_days.value == 3
    assert pre.admission_type.value == "emergency"
    assert [(h.head.value, h.days.value, h.amount.value) for h in pre.estimate] == [
        ("Room rent", Decimal("3.00"), Decimal("9000.00")),
        ("OT charges", None, Decimal("8000.00")),
        ("Surgeon and anaesthetist fees", None, Decimal("9000.00")),
        ("Medicines and consumables", None, Decimal("4000.00")),
    ]
    assert pre.estimate_total.value == Decimal("30000.00")
    # Part C is the source of identity, hospital, admission and clinical until discharge.
    assert record.identity.member_id.value == MEMBER
    assert record.hospital.name.value == "Sunrise Multispeciality Hospital"
    assert record.admission.admission_date.value == ADMITTED
    assert record.admission.accident.value is False
    assert [d.value for d in record.clinical.diagnoses] == ["Acute appendicitis"]
    assert [p.value for p in record.clinical.procedures] == ["Laparoscopic appendicectomy"]
    assert record.clinical.hospital_codes == [
        Code(system="ICD-10", version="as-written", code="K35.8")
    ]
    assert [f.kind for f in record.clinical.facts] == ["finding"]
    assert record.failed_checks == []
    # Only the fields the request's documents supply are scanned: no Part A, bill or cheque.
    assert record.low_confidence_fields == []


async def test_the_estimate_heads_must_sum_to_the_estimate_total(env: Env) -> None:
    env.part_c["estimate_total"] = v("31000")

    _, record = await env.preauth()

    assert failed(record)["preauth_estimate_total"] == ["preauth.estimate_total"]


async def test_the_part_c_rohini_must_match_the_submitting_hospital(env: Env) -> None:
    env.part_c["rohini_id"] = v(HOSPITAL_ROHINI_IDS[1])

    _, record = await env.preauth()

    assert failed(record)["preauth_rohini_matches"] == ["preauth.rohini_id"]


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"expected_stay_days": v(0)}, "preauth.expected_stay_days"),
        ({"admission_date": v("2099-01-01")}, "admission.admission_date"),
    ],
)
async def test_preauth_dates_are_checked(env: Env, change: dict[str, Any], path: str) -> None:
    """An emergency admission cannot start after the request; a stay is at least a day."""
    env.part_c.update(change)

    _, record = await env.preauth()

    assert path in failed(record)["preauth_dates"]


async def test_an_enhancement_is_appended_with_its_interim_bill(env: Env) -> None:
    claim_id, _ = await env.preauth()
    await env.submit(
        RequestType.ENHANCEMENT,
        [(DocType.ENHANCEMENT_REQUEST, "ENHANCEMENT"), (DocType.INTERIM_BILL, "INTERIM")],
        {"extract_enhancement_request": ENHANCEMENT, "extract_interim_bill": INTERIM},
        parent=claim_id,
    )

    record = await env.extract(claim_id)

    [extra] = record.enhancements
    assert extra.extra_amount.value == Decimal("9000.00")
    assert extra.reason.value == "Post-operative ileus; one more day of stay"
    assert extra.extra_days.value == 1
    assert [h.amount.value for h in extra.estimate] == [Decimal("3000.00"), Decimal("6000.00")]
    assert extra.interim_bill_total.value == Decimal("29500.00")
    assert record.interim_bill.grand_total.value == Decimal("29500.00")
    assert record.preauth.estimate_total.value == Decimal("30000.00")  # kept from Part C
    assert "enhancement_total" not in failed(record)


async def test_enhancement_heads_must_sum_to_the_extra_amount(env: Env) -> None:
    claim_id, _ = await env.preauth()
    await env.submit(
        RequestType.ENHANCEMENT,
        [(DocType.ENHANCEMENT_REQUEST, "ENHANCEMENT")],
        {"extract_enhancement_request": {**ENHANCEMENT, "extra_amount": v("9500")}},
        parent=claim_id,
    )

    record = await env.extract(claim_id)

    assert failed(record)["enhancement_total"] == ["enhancements.0.extra_amount"]


async def test_the_discharge_bundle_replaces_clinical_and_fills_the_discharge_date(
    env: Env,
) -> None:
    claim_id, _ = await env.preauth()
    summary = {
        **copy.deepcopy(DISCHARGE),
        "patient_name": v("Imran Qureshi"),
        "discharge_date": v("2026-11-05"),
        "diagnoses": [v("Acute appendicitis with localised peritonitis")],
    }
    bill = {
        **copy.deepcopy(BILL),
        "patient_name": v("Imran Qureshi"),
        "lines": [line("Room Charges", "Semi-private room", "3", "3000", "9000")],
        "section_totals": [section("Room Charges", "9000")],
        "grand_total": v("9000"),
    }
    await env.submit(
        RequestType.DISCHARGE,
        [(DocType.DISCHARGE_SUMMARY, "SUMMARY"), (DocType.FINAL_BILL, "BILL")],
        {"extract_discharge_summary": summary, "extract_final_bill": bill},
        parent=claim_id,
    )

    record = await env.extract(claim_id)

    assert [d.value for d in record.clinical.diagnoses] == [
        "Acute appendicitis with localised peritonitis"
    ]
    assert record.admission.discharge_date.value == date(2026, 11, 5)
    assert record.bill.grand_total.value == Decimal("9000.00")
    assert record.preauth.estimate_total.value == Decimal("30000.00")
    assert record.failed_checks == []
