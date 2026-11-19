from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.classification.contract import (
    ClassificationService,
    DocumentSet,
    UnknownPage,
)
from app.services.classification.prompts import DOC_TYPE_GUIDE
from app.services.classification.service import DefaultClassificationService
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.model_gateway.contract import ModelRole
from app.services.page_prep.service import DefaultPagePreparer
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import DocType, RequestType, Stage
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway

A, B, D = DocType.CLAIM_FORM_PART_A, DocType.FINAL_BILL, DocType.DISCHARGE_SUMMARY


def label(doc_type: DocType, confidence: float) -> dict[str, Any]:
    return {"doc_type": doc_type.value, "confidence": confidence}


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.intake = DefaultIntakeService(sessions, self.files, FakeWorkflowStarter())
        self.gateway = FakeModelGateway({})

    def service(self, labels: list[dict[str, Any]]) -> ClassificationService:
        self.gateway = FakeModelGateway({"classify_page": labels})
        return DefaultClassificationService(
            self.sessions,
            self.files,
            self.gateway,
            DefaultPagePreparer(self.sessions, self.files),
        )

    async def submit(self, *files: tuple[DocType | None, list[str]]) -> Submission:
        return await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=MEMBER_USER_ID,
                channel="member",
                form=ClaimForm(
                    policy_number="P-1",
                    member_id="M-1",
                    patient_name="Asha Verma",
                    admission_date=date(2026, 11, 2),
                    claimed_amount=Decimal("48250"),
                    declaration_accepted=True,
                ),
                files=[
                    UploadedFile(filename=f"f{i}.pdf", data=make_pdf(pages), slot=slot)
                    for i, (slot, pages) in enumerate(files)
                ],
            )
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


def _groups(doc_set: DocumentSet) -> list[tuple[DocType, int, int]]:
    return [(d.doc_type, d.page_range.start, d.page_range.end) for d in doc_set.documents]


async def test_one_call_per_page_and_consecutive_pages_group_into_documents(env: Env) -> None:
    submission = await env.submit((None, ["PART A", "BILL 1", "BILL 2", "DISCHARGE"]))
    service = env.service([label(A, 0.95), label(B, 0.9), label(B, 0.93), label(D, 0.91)])

    doc_set = await service.classify(submission.submission_id)

    assert _groups(doc_set) == [(A, 1, 1), (B, 2, 3), (D, 4, 4)]
    bill = doc_set.documents[1]
    assert bill.page_ids == [doc_set.pages[1].page_id, doc_set.pages[2].page_id]
    assert bill.confidence == 0.9
    assert doc_set.needs_review is False
    assert [c.role for c in env.gateway.calls] == [ModelRole.VISION_CLASSIFY] * 4
    assert all(c.image_count == 1 and c.claim_id == submission.claim_id for c in env.gateway.calls)
    assert env.gateway.calls[1].prompt is not None and "BILL 1" in env.gateway.calls[1].prompt


async def test_a_page_below_point_eight_needs_review(env: Env) -> None:
    submission = await env.submit((None, ["PART A", "BILL"]))
    service = env.service([label(A, 0.95), label(B, 0.79)])

    doc_set = await service.classify(submission.submission_id)

    assert doc_set.needs_review is True


async def test_documents_never_span_two_files(env: Env) -> None:
    submission = await env.submit((B, ["BILL"]), (B, ["BILL AGAIN"]))
    service = env.service([label(B, 0.9), label(B, 0.9)])

    doc_set = await service.classify(submission.submission_id)

    assert _groups(doc_set) == [(B, 1, 1), (B, 2, 2)]


async def test_mismatches_against_the_checklist_and_the_slot_hints(env: Env) -> None:
    submission = await env.submit((B, ["DISCHARGE"]), (None, ["PART A", "MENU CARD"]))
    service = env.service([label(D, 0.9), label(A, 0.9), label(DocType.OTHER, 0.9)])

    doc_set = await service.classify(submission.submission_id)

    found = {(m.kind, m.doc_type) for m in doc_set.mismatches}
    assert found == {
        ("slot_mismatch", D),
        ("unexpected", DocType.OTHER),
        ("missing", DocType.CLAIM_FORM_PART_B),
        ("missing", B),
        ("missing", DocType.PAYMENT_RECEIPT),
        ("missing", DocType.CANCELLED_CHEQUE),
    }
    slot = next(m for m in doc_set.mismatches if m.kind == "slot_mismatch")
    assert slot.page_ids == [doc_set.pages[0].page_id]


async def test_classify_stores_a_version_and_is_idempotent(env: Env) -> None:
    submission = await env.submit((None, ["PART A"]))
    service = env.service([label(A, 0.9)])

    first = await service.classify(submission.submission_id)
    again = await service.classify(submission.submission_id)

    assert again == first
    assert len(env.gateway.calls) == 1
    history = await StageRecordRepository(env.sessions).history(
        submission.claim_id, Stage.CLASSIFICATION
    )
    assert [r.version for r in history] == [1]
    assert DocumentSet.model_validate(history[0].payload) == first


async def test_apply_labels_regroups_and_stores_a_new_version(env: Env) -> None:
    submission = await env.submit((None, ["PART A", "BILL", "BILL 2"]))
    service = env.service([label(A, 0.95), label(D, 0.4), label(B, 0.9)])
    before = await service.classify(submission.submission_id)

    after = await service.apply_labels(
        submission.claim_id, {before.pages[1].page_id: B}, actor="user:ops"
    )

    assert _groups(after) == [(A, 1, 1), (B, 2, 3)]
    assert after.needs_review is False
    assert after.pages[1].labelled_by == "person" and after.pages[1].confidence == 1.0
    latest = await StageRecordRepository(env.sessions).latest(
        submission.claim_id, Stage.CLASSIFICATION
    )
    assert latest is not None and latest.version == 2 and latest.created_by == "user:ops"
    assert await service.classify(submission.submission_id) == after


async def test_apply_labels_refuses_an_unknown_page(env: Env) -> None:
    submission = await env.submit((None, ["PART A"]))
    service = env.service([label(A, 0.9)])
    await service.classify(submission.submission_id)

    with pytest.raises(UnknownPage):
        await service.apply_labels(submission.claim_id, {UUID(int=1): B}, actor="user:ops")


# A merged upload has no slots, so intake cannot list missing documents; classification does.
P, C, R = DocType.CLAIM_FORM_PART_B, DocType.CANCELLED_CHEQUE, DocType.PAYMENT_RECEIPT
BUNDLE = [label(A, 0.95), label(P, 0.95), label(D, 0.95), label(B, 0.95), label(C, 0.95)]


async def _intake_missing(env: Env, submission: Submission) -> tuple[list[str], list[DocType]]:
    """The missing documents on the intake stage record and on the stored submission."""
    record = await StageRecordRepository(env.sessions).latest(submission.claim_id, Stage.INTAKE)
    stored = await env.intake.get(submission.submission_id)
    assert record is not None and stored is not None
    return record.payload["missing_documents"], stored.missing_documents


async def test_a_merged_upload_records_its_missing_documents_after_classification(
    env: Env,
) -> None:
    submission = await env.submit((None, ["PART A", "PART B", "SUMMARY", "BILL", "CHEQUE"]))
    assert submission.missing_documents == []  # intake cannot tell without slots

    await env.service(BUNDLE).classify(submission.submission_id)

    assert await _intake_missing(env, submission) == (["payment_receipt"], [R])


async def test_relabelling_a_merged_upload_updates_its_missing_documents(env: Env) -> None:
    submission = await env.submit((None, ["PART A", "PART B", "SUMMARY", "BILL", "CHEQUE"]))
    service = env.service(BUNDLE)
    before = await service.classify(submission.submission_id)

    await service.apply_labels(submission.claim_id, {before.pages[4].page_id: R}, actor="user:ops")

    assert await _intake_missing(env, submission) == (["cancelled_cheque"], [C])


async def test_slotted_uploads_keep_the_missing_documents_intake_found(env: Env) -> None:
    submission = await env.submit((A, ["PART A"]), (P, ["PART B"]), (D, ["SUMMARY"]),
                                  (B, ["BILL"]), (C, ["CHEQUE"]))  # fmt: skip
    assert submission.missing_documents == [R]
    history_before = await StageRecordRepository(env.sessions).history(
        submission.claim_id, Stage.INTAKE
    )

    await env.service(BUNDLE).classify(submission.submission_id)

    assert await _intake_missing(env, submission) == (["payment_receipt"], [R])
    history = await StageRecordRepository(env.sessions).history(submission.claim_id, Stage.INTAKE)
    assert len(history) == len(history_before)  # nothing new written


def test_every_document_type_has_a_labelling_guide() -> None:
    assert set(DOC_TYPE_GUIDE) == set(DocType)
    assert DocType("ot_notes") in DOC_TYPE_GUIDE
