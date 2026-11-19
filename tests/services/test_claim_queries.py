from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, ReconsiderationRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.claims.contract import ClaimQueryService
from app.services.claims.service import DefaultClaimQueryService
from app.services.communication.service import DefaultCommunicationService
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.page_prep.service import DefaultPagePreparer
from app.services.query.contract import QueryItem
from app.services.query.service import DefaultQueryService
from app.shared.seed_ids import HOSPITAL_USER_ID, MEMBER_USER_ID
from app.shared.types import DocType, RequestType, Stage
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.notifications import FakeNotificationService


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.intake = DefaultIntakeService(sessions, self.files, FakeWorkflowStarter())
        self.claims: ClaimQueryService = DefaultClaimQueryService(sessions)

    async def submit(self, user: UUID, text: str, hospital_id: str | None = None) -> Submission:
        return await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=user,
                channel="member",
                form=ClaimForm(
                    policy_number="P-1",
                    member_id="M-1",
                    patient_name="Asha Verma",
                    hospital_id=hospital_id,
                    admission_date=date(2026, 11, 2),
                    claimed_amount=Decimal("1000"),
                    declaration_accepted=True,
                ),
                files=[
                    UploadedFile(
                        filename="bill.pdf", data=make_pdf([text]), slot=DocType.FINAL_BILL
                    )
                ],
            )
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


async def test_lists_only_the_submitters_claims(env: Env) -> None:
    mine = await env.submit(MEMBER_USER_ID, "one")
    await env.submit(HOSPITAL_USER_ID, "two")

    claims = await env.claims.list_for_submitter(MEMBER_USER_ID)

    assert [(c.claim_id, c.status, c.patient_name) for c in claims] == [
        (mine.claim_id, "received", "Asha Verma")
    ]


async def test_get_shows_the_claim_with_its_submissions_and_duplicates(env: Env) -> None:
    first = await env.submit(MEMBER_USER_ID, "one", hospital_id="8900000000011")
    again = await env.submit(MEMBER_USER_ID, "one", hospital_id="8900000000011")

    detail = await env.claims.get(first.claim_id)

    assert detail is not None
    assert (detail.policy_number, detail.hospital_id) == ("P-1", "8900000000011")
    assert detail.submitted_by == MEMBER_USER_ID
    assert [s.submission_id for s in detail.submissions] == [
        first.submission_id,
        again.submission_id,
    ]
    assert detail.submissions[1].duplicate_of == first.submission_id
    assert detail.submissions[0].files[0].slot is DocType.FINAL_BILL
    assert DocType.CLAIM_FORM_PART_A in detail.submissions[0].missing_documents
    assert await env.claims.get(uuid4()) is None


async def test_stored_file_resolves_uploads_and_page_images(env: Env) -> None:
    submission = await env.submit(MEMBER_USER_ID, "one")
    [page] = await DefaultPagePreparer(env.sessions, env.files).prepare(submission.submission_id)

    upload = await env.claims.stored_file(submission.files[0].storage_key)
    image = await env.claims.stored_file(page.image_key)

    assert upload is not None and upload.mime == "application/pdf"
    assert upload.claim_id == submission.claim_id and upload.policy_number == "P-1"
    assert image is not None and image.mime == "image/png"
    assert image.claim_id == submission.claim_id
    assert await env.claims.stored_file("0" * 32) is None


async def test_get_shows_letters_and_the_open_query_and_letters_resolve_as_files(
    env: Env,
) -> None:
    submission = await env.submit(MEMBER_USER_ID, "one")
    communication = DefaultCommunicationService(
        env.sessions, env.files, FakeModelGateway({}), FakeNotificationService()
    )
    queries = DefaultQueryService(env.sessions, communication, reply_days=15, reminder_days=[3])
    item = QueryItem(text="Payment receipt", source="missing_document")
    query = await queries.raise_query(submission.claim_id, [item])

    detail = await env.claims.get(submission.claim_id)

    assert detail is not None
    [letter] = detail.letters
    assert (letter.kind, letter.letter_id) == ("query", query.letter_id)
    assert letter.download_url == f"/api/files/{letter.pdf_key}"
    assert detail.open_query is not None
    assert (detail.open_query.query_id, detail.open_query.items) == (
        query.query_id,
        ["Payment receipt"],
    )
    assert detail.open_query.deadline == query.deadline
    stored = await env.claims.stored_file(letter.pdf_key)
    assert stored is not None
    assert (stored.mime, stored.claim_id) == ("application/pdf", submission.claim_id)


async def test_pages_map_each_page_to_its_document_and_page_within_it(env: Env) -> None:
    submission = await env.submit(MEMBER_USER_ID, "one")
    assert await env.claims.pages(submission.claim_id) is None  # not classified yet
    doc_id, p1, p2, p3 = uuid4(), uuid4(), uuid4(), uuid4()

    def page(page_id: UUID, seq: int, doc_type: DocType) -> dict[str, object]:
        return {
            "page_id": str(page_id),
            "file_id": str(submission.files[0].file_id),
            "seq": seq,
            "file_page": seq,
            "slot": None,
            "image_key": f"img-{seq}",
            "doc_type": doc_type.value,
            "confidence": 0.9,
        }

    await StageRecordRepository(env.sessions).append(
        submission.claim_id,
        Stage.CLASSIFICATION,
        {
            "claim_id": str(submission.claim_id),
            "submission_id": str(submission.submission_id),
            "pages": [
                page(p1, 1, DocType.FINAL_BILL),
                page(p2, 2, DocType.FINAL_BILL),
                page(p3, 3, DocType.OTHER),
            ],
            "documents": [
                {
                    "document_id": str(doc_id),
                    "doc_type": "final_bill",
                    "page_range": {"start": 1, "end": 2},
                    "page_ids": [str(p1), str(p2)],
                    "confidence": 0.9,
                }
            ],
            "mismatches": [],
            "needs_review": False,
        },
        created_by="model",
    )

    pages = await env.claims.pages(submission.claim_id)

    assert pages is not None
    assert [(p.seq, p.image_key, p.document_id, p.page_in_document) for p in pages.pages] == [
        (1, "img-1", doc_id, 1),
        (2, "img-2", doc_id, 2),
        (3, "img-3", None, None),
    ]
    assert [d.document_id for d in pages.documents] == [doc_id]


async def test_get_shows_a_hold_and_the_reconsiderations(env: Env) -> None:
    submission = await env.submit(MEMBER_USER_ID, "BILL")
    now = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    async with env.sessions.begin() as session:
        claim = await session.get(ClaimRow, submission.claim_id)
        assert claim is not None
        claim.held_at, claim.hold_reason = now, "Hospital under review"
        session.add(
            ReconsiderationRow(
                id=uuid4(), claim_id=submission.claim_id, number=1,
                submission_id=submission.submission_id, workflow_id="wf:reconsideration:1",
                grounds="ICU nursing, not ward nursing", requested_by=MEMBER_USER_ID,
                requested_at=now, previous_decision="partial",
                previous_amount=Decimal("60644.52"), previously_paid=Decimal("60644.52"),
            )
        )  # fmt: skip

    detail = await env.claims.get(submission.claim_id)

    assert detail is not None
    assert (detail.held_at, detail.hold_reason) == (now, "Hospital under review")
    [view] = detail.reconsiderations
    assert (view.number, view.grounds, view.previously_paid) == (
        1,
        "ICU nursing, not ward nursing",
        Decimal("60644.52"),
    )
