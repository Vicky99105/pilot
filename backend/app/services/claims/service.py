"""Default claim queries on the claims database."""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    ClaimRow,
    DocumentChecklistRow,
    FileRow,
    LetterRow,
    PageRow,
    QueryRow,
    ReconsiderationRow,
    SubmissionRow,
)
from app.database.repositories.stage_records import StageRecordRepository
from app.services.claims.contract import (
    AuthorisationView,
    ChecklistItem,
    ClaimDetail,
    ClaimPage,
    ClaimPageDocument,
    ClaimPages,
    ClaimSummary,
    LetterView,
    OpenQueryView,
    ReconsiderationView,
    StoredFile,
    SubmissionView,
    SubmittedFileView,
)
from app.services.classification.contract import DocumentSet
from app.shared.types import DocType, RequestType, Stage


def _summary(claim: ClaimRow, first: SubmissionRow | None) -> ClaimSummary:
    return ClaimSummary(
        claim_id=claim.id,
        claim_number=claim.claim_number,
        request_type=RequestType(claim.request_type),
        status=claim.status,
        patient_name=(first.form or {}).get("patient_name") if first else None,
        deadline=claim.deadline,
        created_at=claim.created_at,
    )


class DefaultClaimQueryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list_for_submitter(self, user_id: UUID) -> list[ClaimSummary]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ClaimRow, SubmissionRow)
                .join(SubmissionRow, SubmissionRow.id == ClaimRow.submission_id)
                .where(SubmissionRow.submitted_by == user_id)
                .order_by(ClaimRow.created_at.desc())
            )
            return [_summary(claim, first) for claim, first in rows]

    async def get(self, claim_id: UUID) -> ClaimDetail | None:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
            if claim is None:
                return None
            submissions = list(
                await session.scalars(
                    select(SubmissionRow)
                    .where(SubmissionRow.claim_id == claim_id)
                    .order_by(SubmissionRow.created_at)
                )
            )
            files = list(
                await session.scalars(
                    select(FileRow)
                    .where(FileRow.submission_id.in_([s.id for s in submissions]))
                    .order_by(FileRow.position)
                )
            )
            letters = list(
                await session.scalars(
                    select(LetterRow)
                    .where(LetterRow.claim_id == claim_id)
                    .order_by(LetterRow.created_at)
                )
            )
            query = await session.scalar(
                select(QueryRow)
                .where(QueryRow.claim_id == claim_id, QueryRow.status.in_(("open", "overdue")))
                .order_by(QueryRow.raised_at.desc())
                .limit(1)
            )
            reconsiderations = list(
                await session.scalars(
                    select(ReconsiderationRow)
                    .where(ReconsiderationRow.claim_id == claim_id)
                    .order_by(ReconsiderationRow.number)
                )
            )
        first = next((s for s in submissions if s.id == claim.submission_id), None)
        authorised = await StageRecordRepository(self._sessions).history(
            claim_id, Stage.AUTHORISATION
        )
        return ClaimDetail(
            current_request=RequestType(claim.current_request) if claim.current_request else None,
            # The clock runs until the current request is authorised.
            clock_deadline=claim.deadline
            if claim.current_request
            and not any(r.payload.get("request_type") == claim.current_request for r in authorised)
            else None,
            authorisations=[
                AuthorisationView.model_validate(r.payload, from_attributes=False)
                for r in authorised
            ],
            originals_due_at=claim.originals_due_at,
            originals_received_at=claim.originals_received_at,
            held_at=claim.held_at,
            hold_reason=claim.hold_reason,
            reconsiderations=[
                ReconsiderationView(
                    number=r.number,
                    grounds=r.grounds,
                    requested_at=r.requested_at,
                    previous_decision=r.previous_decision,
                    previous_amount=r.previous_amount,
                    previously_paid=r.previously_paid,
                )
                for r in reconsiderations
            ],
            **_summary(claim, first).model_dump(),
            policy_number=claim.policy_number,
            member_id=claim.member_id,
            hospital_id=claim.hospital_id,
            submitted_by=first.submitted_by if first else None,
            closed_at=claim.closed_at,
            letters=[
                LetterView(
                    letter_id=letter.id,
                    kind=letter.kind,
                    created_at=letter.created_at,
                    pdf_key=letter.pdf_key,
                    download_url=f"/api/files/{letter.pdf_key}",
                    sent=letter.sent,
                )
                for letter in letters
            ],
            open_query=OpenQueryView(
                query_id=query.id,
                items=[item["text"] for item in query.items],
                status=query.status,
                raised_at=query.raised_at,
                deadline=query.deadline,
                reply_url=f"/api/queries/{query.id}/reply",
            )
            if query
            else None,
            submissions=[
                SubmissionView(
                    submission_id=s.id,
                    request_type=RequestType(s.request_type),
                    created_at=s.created_at,
                    duplicate_of=s.duplicate_of,
                    files=[
                        SubmittedFileView(
                            file_id=f.id,
                            filename=f.filename,
                            slot=DocType(f.slot_hint) if f.slot_hint else None,
                            mime=f.mime,
                            size_bytes=f.size_bytes,
                            storage_key=f.storage_key,
                            quality_warning=f.quality_warning,
                        )
                        for f in files
                        if f.submission_id == s.id
                    ],
                    missing_documents=[DocType(d) for d in s.missing_documents],
                    warnings=s.warnings,
                )
                for s in submissions
            ],
        )

    async def pages(self, claim_id: UUID) -> ClaimPages | None:
        record = await StageRecordRepository(self._sessions).latest(claim_id, Stage.CLASSIFICATION)
        if record is None:
            return None
        doc_set = DocumentSet.model_validate(record.payload)
        place = {
            page_id: (d.document_id, n)
            for d in doc_set.documents
            for n, page_id in enumerate(d.page_ids, start=1)
        }
        return ClaimPages(
            claim_id=claim_id,
            classification_version=record.version,
            pages=[
                ClaimPage(
                    page_id=p.page_id,
                    seq=p.seq,
                    doc_type=p.doc_type,
                    confidence=p.confidence,
                    image_key=p.image_key,
                    quality_warning=p.quality_warning,
                    document_id=place[p.page_id][0] if p.page_id in place else None,
                    page_in_document=place[p.page_id][1] if p.page_id in place else None,
                )
                for p in sorted(doc_set.pages, key=lambda p: p.seq)
            ],
            documents=[
                ClaimPageDocument(
                    document_id=d.document_id, doc_type=d.doc_type, page_ids=d.page_ids
                )
                for d in doc_set.documents
            ],
        )

    async def checklist(self, request_type: RequestType) -> list[ChecklistItem]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(DocumentChecklistRow)
                .where(DocumentChecklistRow.request_type == request_type.value)
                .order_by(DocumentChecklistRow.id)
            )
            return [
                ChecklistItem(
                    doc_type=DocType(r.doc_type),
                    required=r.required,
                    mandatory_from_amount=r.mandatory_from_amount,
                    notes=r.notes,
                )
                for r in rows
            ]

    async def stored_file(self, storage_key: str) -> StoredFile | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(FileRow, PageRow.image_key, ClaimRow)
                    .outerjoin(PageRow, PageRow.file_id == FileRow.id)
                    .join(SubmissionRow, SubmissionRow.id == FileRow.submission_id)
                    .join(ClaimRow, ClaimRow.id == SubmissionRow.claim_id)
                    .where(
                        or_(FileRow.storage_key == storage_key, PageRow.image_key == storage_key)
                    )
                    .limit(1)
                )
            ).first()
        if row is None:
            return await self._letter_file(storage_key)
        file, _, claim = row
        return StoredFile(
            storage_key=storage_key,
            mime=file.mime if file.storage_key == storage_key else "image/png",
            claim_id=claim.id,
            policy_number=claim.policy_number,
            hospital_id=claim.hospital_id,
        )

    async def _letter_file(self, storage_key: str) -> StoredFile | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(LetterRow, ClaimRow)
                    .join(ClaimRow, ClaimRow.id == LetterRow.claim_id)
                    .where(LetterRow.pdf_key == storage_key)
                )
            ).first()
        if row is None:
            return None
        _, claim = row
        return StoredFile(
            storage_key=storage_key,
            mime="application/pdf",
            claim_id=claim.id,
            policy_number=claim.policy_number,
            hospital_id=claim.hospital_id,
        )
