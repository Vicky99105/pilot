import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.intake.contract import (
    MB,
    Channel,
    ClaimForm,
    IntakeService,
    SubmissionInput,
    SubmissionRejected,
    UploadedFile,
)
from app.services.intake.service import DefaultIntakeService
from app.shared.seed_ids import MEMBER_MEMBER_ID, MEMBER_POLICY_NUMBER, MEMBER_USER_ID
from app.shared.types import DocType, RequestType, Stage
from tests.docs import make_image, make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter

NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
PDF = make_pdf(["Claim form Part A"])


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.starter = FakeWorkflowStarter()
        self.intake: IntakeService = DefaultIntakeService(
            sessions, self.files, self.starter, signaller=self.starter, now=lambda: NOW
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


def _form(**overrides: Any) -> ClaimForm:
    fields: dict[str, Any] = {
        "policy_number": MEMBER_POLICY_NUMBER,
        "member_id": MEMBER_MEMBER_ID,
        "patient_name": "Asha Verma",
        "admission_date": date(2026, 11, 2),
        "discharge_date": date(2026, 11, 5),
        "claimed_amount": Decimal("48250"),
    }
    return ClaimForm(**(fields | overrides))


def _submission(
    files: list[UploadedFile],
    *,
    request_type: RequestType = RequestType.REIMBURSEMENT,
    channel: Channel = "member",
    **form: Any,
) -> SubmissionInput:
    return SubmissionInput(
        request_type=request_type,
        submitted_by=MEMBER_USER_ID,
        channel=channel,
        form=_form(**form),
        files=files,
    )


def _file(slot: DocType | None, data: bytes = PDF, name: str = "doc.pdf") -> UploadedFile:
    return UploadedFile(filename=name, content_type="application/pdf", data=data, slot=slot)


TYPED_SLOTS = [DocType.CLAIM_FORM_PART_A, DocType.FINAL_BILL, DocType.DISCHARGE_SUMMARY]


def _typed_files(marker: str = "") -> list[UploadedFile]:
    return [_file(slot, make_pdf([f"{slot.value} {marker}"])) for slot in TYPED_SLOTS]


async def test_accept_stores_files_creates_claim_and_starts_its_workflow(env: Env) -> None:
    submission = await env.intake.accept(_submission(_typed_files()))

    assert [f.slot for f in submission.files] == TYPED_SLOTS
    first = submission.files[0]
    assert first.sha256 == hashlib.sha256(env.files.files[first.storage_key][0]).hexdigest()
    assert first.mime == "application/pdf"
    assert submission.deadline == NOW + timedelta(days=15)
    assert submission.workflow_id == str(submission.claim_id)
    assert env.starter.started == {
        str(submission.claim_id): (submission.submission_id, RequestType.REIMBURSEMENT)
    }
    assert await env.intake.get(submission.submission_id) == submission
    record = await StageRecordRepository(env.sessions).latest(submission.claim_id, Stage.INTAKE)
    assert record is not None and record.payload["submission_id"] == str(submission.submission_id)


async def test_missing_documents_come_from_the_checklist_and_never_block(env: Env) -> None:
    submission = await env.intake.accept(_submission(_typed_files()))

    assert submission.missing_documents == [
        DocType.CLAIM_FORM_PART_B,
        DocType.PAYMENT_RECEIPT,
        DocType.CANCELLED_CHEQUE,
    ]
    assert submission.workflow_id is not None


async def test_kyc_is_required_only_from_one_lakh(env: Env) -> None:
    below = await env.intake.accept(_submission(_typed_files("a"), claimed_amount="99999.99"))
    at = await env.intake.accept(_submission(_typed_files("b"), claimed_amount="100000"))

    assert DocType.KYC not in below.missing_documents
    assert DocType.KYC in at.missing_documents


async def test_merged_pdf_defers_the_missing_documents_check(env: Env) -> None:
    submission = await env.intake.accept(_submission([_file(None, name="all.pdf")]))

    assert submission.missing_documents == []
    assert any("after classification" in w for w in submission.warnings)


async def test_same_files_member_and_admission_date_link_to_the_earlier_submission(
    env: Env,
) -> None:
    first = await env.intake.accept(_submission(_typed_files()))
    again = await env.intake.accept(_submission(list(reversed(_typed_files()))))
    other_day = await env.intake.accept(
        _submission(_typed_files(), admission_date=date(2026, 11, 3))
    )

    assert again.duplicate_of == first.submission_id
    assert again.claim_id == first.claim_id
    assert again.files == []
    assert other_day.duplicate_of is None and other_day.claim_id != first.claim_id
    assert len(env.starter.started) == 2


async def test_a_duplicate_starts_the_workflow_when_the_first_start_failed(env: Env) -> None:
    env.starter.unavailable = True
    first = await env.intake.accept(_submission(_typed_files()))
    env.starter.unavailable = False
    again = await env.intake.accept(_submission(_typed_files()))

    assert first.workflow_id is None
    assert any("not started" in w for w in first.warnings)
    assert again.workflow_id == str(first.claim_id)
    assert list(env.starter.started) == [str(first.claim_id)]


@pytest.mark.parametrize(
    ("request_type", "window"),
    [
        (RequestType.PREAUTH, timedelta(hours=1)),
        (RequestType.ENHANCEMENT, timedelta(hours=1)),
        (RequestType.DISCHARGE, timedelta(hours=3)),
        (RequestType.REIMBURSEMENT, timedelta(days=15)),
    ],
)
async def test_deadline_follows_the_request_type(
    env: Env, request_type: RequestType, window: timedelta
) -> None:
    submission = await env.intake.accept(
        _submission(_typed_files(), request_type=request_type, channel="hospital")
    )

    assert submission.deadline == NOW + window


@pytest.mark.parametrize(
    ("fmt", "mime"), [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("TIFF", "image/tiff")]
)
async def test_images_are_accepted(env: Env, fmt: str, mime: str) -> None:
    data = make_image(fmt, (200, 300))

    submission = await env.intake.accept(_submission([_file(DocType.FINAL_BILL, data, "b")]))

    assert submission.files[0].mime == mime


async def test_heic_is_converted_to_jpeg_and_hashed_as_uploaded(env: Env) -> None:
    heic = make_image("HEIC", (64, 48))

    submission = await env.intake.accept(
        _submission([_file(DocType.FINAL_BILL, heic, "photo.heic")])
    )

    stored = submission.files[0]
    assert stored.mime == "image/jpeg"
    assert env.files.files[stored.storage_key][0].startswith(b"\xff\xd8")
    assert stored.sha256 == hashlib.sha256(heic).hexdigest()
    assert any("photo.heic" in w and "JPEG" in w for w in submission.warnings)


async def test_unsupported_types_are_rejected_with_a_clear_reason(env: Env) -> None:
    files = [
        _file(DocType.FINAL_BILL),
        _file(DocType.KYC, b"PK\x03\x04 a word file", "aadhaar.docx"),
    ]

    with pytest.raises(SubmissionRejected) as rejected:
        await env.intake.accept(_submission(files))

    assert rejected.value.reasons == [
        "aadhaar.docx: unsupported file type; upload PDF, JPG, PNG, TIFF or HEIC"
    ]
    assert env.files.files == {} and env.starter.started == {}


async def test_corrupt_heic_is_rejected(env: Env) -> None:
    fake_heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64

    with pytest.raises(SubmissionRejected) as rejected:
        await env.intake.accept(_submission([_file(DocType.KYC, fake_heic, "id.heic")]))

    assert rejected.value.reasons == ["id.heic: HEIC image could not be read; upload a JPG"]


@pytest.mark.parametrize(("channel", "limit_mb"), [("member", 10), ("hospital", 15)])
async def test_files_over_the_channel_limit_are_rejected(
    env: Env, channel: Channel, limit_mb: int
) -> None:
    at_limit = b"%PDF-" + b"0" * (limit_mb * MB - 5)
    over = at_limit + b"0"

    ok = await env.intake.accept(
        _submission([_file(DocType.FINAL_BILL, at_limit)], channel=channel)
    )
    with pytest.raises(SubmissionRejected) as rejected:
        await env.intake.accept(_submission([_file(DocType.FINAL_BILL, over)], channel=channel))

    assert ok.files[0].size_bytes == limit_mb * MB
    assert rejected.value.reasons == [f"doc.pdf: larger than the {limit_mb} MB limit"]


async def test_an_empty_submission_is_rejected(env: Env) -> None:
    with pytest.raises(SubmissionRejected):
        await env.intake.accept(_submission([]))


async def test_a_follow_up_joins_its_parent_claim_without_a_new_workflow(env: Env) -> None:
    parent = await env.intake.accept(_submission(_typed_files()))
    follow_up = SubmissionInput(
        request_type=RequestType.QUERY_REPLY,
        submitted_by=MEMBER_USER_ID,
        channel="member",
        form=_form(),
        files=[_file(DocType.CANCELLED_CHEQUE, make_pdf(["cheque"]))],
        parent_claim_id=parent.claim_id,
    )

    reply = await env.intake.accept(follow_up)

    assert reply.claim_id == parent.claim_id
    assert reply.parent_claim_id == parent.claim_id
    assert list(env.starter.started) == [str(parent.claim_id)]
    # The parent's workflow hears about the follow-up.
    assert env.starter.followups == [(str(parent.claim_id), reply.submission_id)]


async def test_a_follow_up_copies_the_parents_form(env: Env) -> None:
    parent = await env.intake.accept(_submission(_typed_files()))

    reply = await env.intake.accept_followup(
        parent.claim_id,
        submitted_by=MEMBER_USER_ID,
        channel="member",
        files=[_file(DocType.PAYMENT_RECEIPT, make_pdf(["receipt"]))],
    )

    assert (reply.request_type, reply.form) == (RequestType.QUERY_REPLY, parent.form)
    assert (reply.claim_id, reply.missing_documents) == (parent.claim_id, [])
    assert env.starter.followups == [(str(parent.claim_id), reply.submission_id)]


async def test_a_follow_up_is_kept_with_a_warning_when_the_signal_fails(env: Env) -> None:
    parent = await env.intake.accept(_submission(_typed_files()))
    env.starter.unavailable = True

    reply = await env.intake.accept_followup(
        parent.claim_id,
        submitted_by=MEMBER_USER_ID,
        channel="member",
        files=[_file(DocType.PAYMENT_RECEIPT, make_pdf(["receipt"]))],
    )

    assert reply.claim_id == parent.claim_id
    assert any("not delivered" in w for w in reply.warnings)


async def test_get_returns_none_for_an_unknown_submission(env: Env) -> None:
    assert await env.intake.get(UUID(int=7)) is None
