import copy
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.classification.service import DefaultClassificationService
from app.services.digitisation.contract import (
    ClaimRecord,
    DigitisationService,
    InvalidCorrection,
    NotClassified,
)
from app.services.digitisation.service import DefaultDigitisationService
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.model_gateway.contract import ModelRole
from app.services.page_prep.service import DefaultPagePreparer
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import Code, DocType, PageRef, RequestType, Stage
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway

POLICY = "MOCK/AS/2026/000101"


def v(value: Any, confidence: float = 0.97, page: int = 1, handwritten: bool = False) -> Any:
    return {"value": value, "confidence": confidence, "page": page, "handwritten": handwritten}


def line(head: str, desc: str, qty: str, rate: str, amount: str, page: int = 1) -> Any:
    return {
        "section": head,
        "date": "2026-11-02",
        "description": desc,
        "qty": qty,
        "rate": rate,
        "amount": amount,
        "page": page,
        "confidence": 0.96,
        "handwritten": False,
    }


def section(head: str, amount: str) -> Any:
    return {"section": head, "amount": amount, "page": 2, "confidence": 0.96}


PART_A: dict[str, Any] = {
    "patient_name": v("Asha Verma"),
    "policy_number": v(POLICY),
    "member_id": v("MOCK-M-000101-01"),
    "date_of_birth": v("1988-04-12"),
    "gender": v("F"),
    "hospital_name": v("Sunrise Multispeciality Hospital"),
    "hospital_city": v("New Delhi"),
    "admission_date": v("2026-11-02"),
    "discharge_date": v("2026-11-05"),
    "hospitalisation_due_to_injury": v(False),
    "claimed_hospital_expenses": v("48250"),
    "claimed_pre_hospitalisation": v(None),
    "claimed_post_hospitalisation": v(None),
    "claimed_total": v("48,250.00"),
    "bank_account_number": v("1234 5678 9012"),
    "ifsc": v("SBIN0001234"),
    "account_holder": v("Asha Verma"),
}
DISCHARGE: dict[str, Any] = {
    "patient_name": v("Mrs. ASHA VERMA"),
    "admission_date": v("2026-11-02"),
    "discharge_date": v("2026-11-05"),
    "diagnoses": [v("Acute gastroenteritis with moderate dehydration")],
    "procedures": [],
    "icd_codes_written": [v("A09")],
    "facts": [
        {
            "kind": "history",
            "text": "k/c/o type 2 diabetes mellitus",
            "duration": "since 5 years",
            "page": 1,
            "confidence": 0.9,
        }
    ],
}
BILL: dict[str, Any] = {
    "bill_number": v("SMH/IP/2026/1182"),
    "patient_name": v("Asha Verma"),
    "lines": [
        line("Room Charges", "Twin sharing room", "3", "4000", "12000"),
        line("Professional Fees", "Physician visit", "4", "1500", "6000"),
        line("Pharmacy", "IV fluids and drugs", "1", "8250", "8250", page=2),
        line("Investigations", "CBC, KFT, stool culture", "1", "22000", "22000", page=2),
    ],
    "section_totals": [
        section("Room Charges", "12000"),
        section("Professional Fees", "6000"),
        section("Pharmacy", "8250"),
        section("Investigations", "22000"),
    ],
    "grand_total": v("48250", page=2),
}
CHEQUE: dict[str, Any] = {
    "account_number": v("123456789012"),
    "ifsc": v("sbin0001234"),
    "account_holder": v("ASHA VERMA"),
    "bank_name": v("State Bank of India"),
}
# One page per document, except the bill, which has two.
PAGES = [
    (DocType.CLAIM_FORM_PART_A, "PART A"),
    (DocType.DISCHARGE_SUMMARY, "DISCHARGE SUMMARY"),
    (DocType.FINAL_BILL, "FINAL BILL page 1"),
    (DocType.FINAL_BILL, "FINAL BILL page 2"),
    (DocType.CANCELLED_CHEQUE, "CHEQUE"),
]


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.intake = DefaultIntakeService(sessions, self.files, FakeWorkflowStarter())
        self.replies = {
            DocType.CLAIM_FORM_PART_A: copy.deepcopy(PART_A),
            DocType.DISCHARGE_SUMMARY: copy.deepcopy(DISCHARGE),
            DocType.FINAL_BILL: copy.deepcopy(BILL),
            DocType.CANCELLED_CHEQUE: copy.deepcopy(CHEQUE),
        }
        self.gateway = FakeModelGateway({})

    async def claim(
        self,
        pages: list[tuple[DocType, str]] = PAGES,
        claimed_amount: str = "48250",
    ) -> Submission:
        submission = await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=MEMBER_USER_ID,
                channel="member",
                form=ClaimForm(
                    policy_number=POLICY,
                    member_id="MOCK-M-000101-01",
                    patient_name="Asha Verma",
                    admission_date=date(2026, 11, 2),
                    discharge_date=date(2026, 11, 5),
                    claimed_amount=Decimal(claimed_amount),
                ),
                files=[
                    UploadedFile(filename="all.pdf", data=make_pdf([text for _, text in pages]))
                ],
            )
        )
        types = {t for t, _ in pages}
        self.gateway = FakeModelGateway(
            {
                "classify_page": [{"doc_type": t.value, "confidence": 0.95} for t, _ in pages],
                **{f"extract_{t.value}": [r] for t, r in self.replies.items() if t in types},
            }
        )
        classification = DefaultClassificationService(
            self.sessions, self.files, self.gateway, DefaultPagePreparer(self.sessions, self.files)
        )
        await classification.classify(submission.submission_id)
        return submission

    @property
    def service(self) -> DigitisationService:
        return DefaultDigitisationService(self.sessions, self.files, self.gateway)

    async def extract(self, **kwargs: Any) -> ClaimRecord:
        submission = await self.claim(**kwargs)
        return await self.service.extract(submission.claim_id)


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


def failed(record: ClaimRecord) -> dict[str, list[str]]:
    return {c.check_id: c.fields for c in record.failed_checks}


async def test_a_clean_claim_assembles_one_record_with_page_sources(env: Env) -> None:
    record = await env.extract()

    part_a = record.documents[DocType.CLAIM_FORM_PART_A]
    bill = record.documents[DocType.FINAL_BILL]
    assert record.identity.patient_name.value == "Asha Verma"
    assert record.identity.patient_name.source == PageRef(document_id=part_a, page=1)
    assert record.hospital.city.value == "New Delhi"
    assert record.admission.admission_date.value == date(2026, 11, 2)
    assert record.admission.accident.value is False
    assert record.claimed.total.value == Decimal("48250.00")
    assert record.claimed.pre_hospitalisation.value is None
    assert record.clinical.diagnoses[0].value == "Acute gastroenteritis with moderate dehydration"
    assert record.clinical.hospital_codes == [
        Code(system="ICD-10", version="as-written", code="A09")
    ]
    fact = record.clinical.facts[0]
    assert (fact.kind, fact.duration) == ("history", "since 5 years")
    assert fact.source is not None
    assert [ln.amount.value for ln in record.bill.lines] == [
        Decimal("12000.00"),
        Decimal("6000.00"),
        Decimal("8250.00"),
        Decimal("22000.00"),
    ]
    assert record.bill.lines[2].head.value == "Pharmacy"
    assert record.bill.lines[2].amount.source == PageRef(document_id=bill, page=2)
    assert record.bank.ifsc.value == "SBIN0001234"
    assert record.claim_form_bank.account_number.value == "1234 5678 9012"
    assert record.failed_checks == []
    assert record.low_confidence_fields == []


async def test_one_extract_call_per_document_with_all_its_pages(env: Env) -> None:
    await env.extract()

    extract_calls = [c for c in env.gateway.calls if c.role is ModelRole.VISION_EXTRACT]
    assert [(c.purpose, c.image_count) for c in extract_calls] == [
        ("extract_claim_form_part_a", 1),
        ("extract_discharge_summary", 1),
        ("extract_final_bill", 2),
        ("extract_cancelled_cheque", 1),
    ]


async def test_extract_stores_a_version_and_is_idempotent(env: Env) -> None:
    submission = await env.claim()
    first = await env.service.extract(submission.claim_id)
    calls = len(env.gateway.calls)

    again = await env.service.extract(submission.claim_id)

    assert again == first and len(env.gateway.calls) == calls
    latest = await StageRecordRepository(env.sessions).latest(
        submission.claim_id, Stage.DIGITISATION
    )
    assert latest is not None and latest.version == 1
    assert ClaimRecord.model_validate(latest.payload) == first


async def test_bill_lines_must_sum_to_the_grand_total_within_one_rupee(env: Env) -> None:
    env.replies[DocType.FINAL_BILL]["grand_total"] = v("48251.00")
    within = await env.extract()
    env.replies[DocType.FINAL_BILL]["grand_total"] = v("49250")
    beyond = await env.extract(pages=[*PAGES, (DocType.OTHER, "x")])

    assert within.failed_checks == []
    assert failed(beyond) == {"bill_grand_total": ["bill.grand_total"]}


async def test_bill_lines_must_sum_to_their_section_total(env: Env) -> None:
    env.replies[DocType.FINAL_BILL]["section_totals"][2] = section("Pharmacy", "8000")

    record = await env.extract()

    assert failed(record) == {"bill_section_totals": ["bill.section_totals.2.total"]}
    assert "Pharmacy" in record.failed_checks[0].detail


async def test_admission_must_not_be_after_discharge(env: Env) -> None:
    env.replies[DocType.CLAIM_FORM_PART_A]["discharge_date"] = v("2026-11-01")

    record = await env.extract()

    assert failed(record)["dates_in_order"] == [
        "admission.admission_date",
        "admission.discharge_date",
    ]


async def test_patient_name_must_match_across_documents(env: Env) -> None:
    env.replies[DocType.DISCHARGE_SUMMARY]["patient_name"] = v("Rekha Sharma")

    record = await env.extract()

    assert failed(record) == {"patient_name_matches": ["clinical.patient_name"]}


async def test_form_fields_must_match_the_documents(env: Env) -> None:
    record = await env.extract(claimed_amount="50000")

    assert failed(record) == {"form_matches_documents": ["claimed.total"]}


async def test_claim_form_bank_must_match_the_cheque(env: Env) -> None:
    env.replies[DocType.CANCELLED_CHEQUE]["account_number"] = v("999956789012")

    record = await env.extract()

    assert failed(record) == {"bank_matches_cheque": ["bank.account_number"]}


async def test_low_confidence_uses_a_stricter_bar_for_handwriting(env: Env) -> None:
    part_a = env.replies[DocType.CLAIM_FORM_PART_A]
    part_a["patient_name"] = v("Asha Verma", confidence=0.84)
    part_a["member_id"] = v("MOCK-M-000101-01", confidence=0.9, handwritten=True)
    part_a["gender"] = v("F", confidence=0.9)

    record = await env.extract()

    assert record.low_confidence_fields == ["identity.patient_name", "identity.member_id"]


async def test_a_missing_document_leaves_its_fields_for_an_operator(env: Env) -> None:
    record = await env.extract(pages=PAGES[:-1])

    assert DocType.CANCELLED_CHEQUE not in record.documents
    assert record.bank.account_number.value is None
    assert {"bank.account_number", "bank.ifsc", "bank.account_holder"} <= set(
        record.low_confidence_fields
    )
    assert "bank_matches_cheque" not in failed(record)


async def test_corrections_rerun_the_checks_and_store_a_new_version(env: Env) -> None:
    env.replies[DocType.FINAL_BILL]["grand_total"] = v("49250", confidence=0.7)
    submission = await env.claim()
    before = await env.service.extract(submission.claim_id)

    after = await env.service.apply_corrections(
        submission.claim_id, {"bill.grand_total": "48250"}, actor="user:ops"
    )

    assert "bill_grand_total" in failed(before)
    assert "bill.grand_total" in before.low_confidence_fields
    assert after.failed_checks == [] and after.low_confidence_fields == []
    assert after.bill.grand_total.value == Decimal("48250.00")
    assert after.bill.grand_total.confidence == 1.0
    assert after.corrected_fields == ["bill.grand_total"]
    history = await StageRecordRepository(env.sessions).history(
        submission.claim_id, Stage.DIGITISATION
    )
    assert [(r.version, r.created_by) for r in history] == [(1, "model"), (2, "user:ops")]
    assert await env.service.extract(submission.claim_id) == after


@pytest.mark.parametrize(
    "fields",
    [
        {"identity.no_such_field": "x"},
        {"bill.lines.9.amount": "1"},
        {"admission.admission_date": "not a date"},
        {"identity": "x"},
    ],
)
async def test_invalid_corrections_are_refused(env: Env, fields: dict[str, Any]) -> None:
    submission = await env.claim()
    await env.service.extract(submission.claim_id)

    with pytest.raises(InvalidCorrection):
        await env.service.apply_corrections(submission.claim_id, fields, actor="user:ops")


async def test_extract_needs_a_classified_claim(env: Env) -> None:
    with pytest.raises(NotClassified):
        await env.service.extract(UUID(int=5))


async def test_a_reply_is_merged_into_a_new_record_version(env: Env) -> None:
    """A query reply is classified on its own. Extraction reads only the reply's documents
    and merges them over the claim's record: unchanged sections stay as they were."""
    submission = await env.claim(pages=[p for p in PAGES if p[0] != DocType.CANCELLED_CHEQUE])
    first = await env.service.extract(submission.claim_id)
    assert first.bank.ifsc.value is None  # no cheque yet

    reply = await env.intake.accept(
        SubmissionInput(
            request_type=RequestType.QUERY_REPLY,
            submitted_by=MEMBER_USER_ID,
            channel="member",
            parent_claim_id=submission.claim_id,
            form=submission.form,
            files=[UploadedFile(filename="reply.pdf", data=make_pdf(["RECEIPT", "CHEQUE"]))],
        )
    )
    gateway = FakeModelGateway(
        {
            "classify_page": [
                {"doc_type": "payment_receipt", "confidence": 0.95},
                {"doc_type": "cancelled_cheque", "confidence": 0.95},
            ],
            "extract_cancelled_cheque": [CHEQUE],
        }
    )
    classification = DefaultClassificationService(
        env.sessions, env.files, gateway, DefaultPagePreparer(env.sessions, env.files)
    )
    reply_set = await classification.classify(reply.submission_id)

    merged = await DefaultDigitisationService(env.sessions, env.files, gateway).extract(
        submission.claim_id
    )

    assert [c.purpose for c in gateway.calls if c.role is ModelRole.VISION_EXTRACT] == [
        "extract_cancelled_cheque"
    ]
    cheque_doc = next(d for d in reply_set.documents if d.doc_type is DocType.CANCELLED_CHEQUE)
    assert merged.classification_version == 2
    assert merged.bank.ifsc.value == "SBIN0001234"
    assert merged.bank.ifsc.source == PageRef(document_id=cheque_doc.document_id, page=1)
    assert merged.documents[DocType.CANCELLED_CHEQUE] == cheque_doc.document_id
    assert merged.documents[DocType.FINAL_BILL] == first.documents[DocType.FINAL_BILL]
    assert (merged.identity, merged.bill, merged.clinical) == (
        first.identity,
        first.bill,
        first.clinical,
    )
    history = await StageRecordRepository(env.sessions).history(
        submission.claim_id, Stage.DIGITISATION
    )
    assert [r.version for r in history] == [1, 2]
