"""Default classification: one vision call per page, then grouping and checklist checks."""

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    ClaimRow,
    DocumentRow,
    FileRow,
    PageRow,
    StageRecordRow,
    SubmissionRow,
)
from app.database.repositories.document_checklists import checklist
from app.database.repositories.overrides import OverrideRepository
from app.database.repositories.stage_records import StageRecordRepository, append_in_session
from app.services.classification import prompts
from app.services.classification._helpers import group_pages, mismatches, small_jpeg
from app.services.classification.contract import (
    REVIEW_BELOW,
    ClassifiedDocument,
    DocumentSet,
    LabelledPage,
    PageRange,
    UnknownPage,
)
from app.services.file_store.contract import FileStore
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.overrides.contract import OverrideEntry
from app.services.page_prep.contract import PagePreparer, PreparedPage
from app.shared.errors import PermanentError
from app.shared.types import DocType, RequestType, Stage


class SubmissionNotFound(PermanentError):
    pass


class DefaultClassificationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        file_store: FileStore,
        gateway: ModelGateway,
        pages: PagePreparer,
    ) -> None:
        self._sessions = sessions
        self._files = file_store
        self._gateway = gateway
        self._pages = pages
        self._records = StageRecordRepository(sessions)

    async def classify(self, submission_id: UUID) -> DocumentSet:
        """Idempotent: the claim's latest set for this submission is returned as stored,
        including any labels a person has corrected."""
        claim_id = await self._claim_of(submission_id)
        latest = await self._records.latest(claim_id, Stage.CLASSIFICATION)
        if latest is not None and latest.payload["submission_id"] == str(submission_id):
            return DocumentSet.model_validate(latest.payload)
        prepared = await self._pages.prepare(submission_id)
        labelled = [await self._label(claim_id, page) for page in prepared]
        return await self._store(claim_id, submission_id, labelled, created_by="model")

    async def apply_labels(
        self, claim_id: UUID, labels: dict[UUID, DocType], *, actor: str
    ) -> DocumentSet:
        latest = await self._records.latest(claim_id, Stage.CLASSIFICATION)
        if latest is None:
            raise UnknownPage(f"claim {claim_id} has no classified pages")
        current = DocumentSet.model_validate(latest.payload)
        unknown = set(labels) - {p.page_id for p in current.pages}
        if unknown:
            raise UnknownPage(", ".join(sorted(str(u) for u in unknown)))
        pages = [
            p.model_copy(
                update={"doc_type": labels[p.page_id], "confidence": 1.0, "labelled_by": "person"}
            )
            if p.page_id in labels
            else p
            for p in current.pages
        ]
        stored = await self._store(claim_id, current.submission_id, pages, created_by=actor)
        await OverrideRepository(self._sessions).append(
            [
                OverrideEntry(
                    claim_id=claim_id,
                    stage=Stage.CLASSIFICATION,
                    kind="label_fix",
                    field=f"page {p.seq}",
                    before=p.doc_type.value,
                    after=labels[p.page_id].value,
                    reason="",
                    actor=actor,
                )
                for p in current.pages
                if p.page_id in labels and labels[p.page_id] != p.doc_type
            ]
        )
        return stored

    async def _claim_of(self, submission_id: UUID) -> UUID:
        async with self._sessions() as session:
            row = await session.get(SubmissionRow, submission_id)
        if row is None or row.claim_id is None:
            raise SubmissionNotFound(str(submission_id))
        return row.claim_id

    async def _label(self, claim_id: UUID, page: PreparedPage) -> LabelledPage:
        png = await self._files.get(page.image_key)
        image = await asyncio.to_thread(small_jpeg, png)
        reply = await self._gateway.vision(
            ModelRole.VISION_CLASSIFY,
            prompts.page_prompt(page.slot, page.text_layer),
            [image],
            prompts.PageLabelReply,
            claim_id=claim_id,
            purpose=prompts.PURPOSE,
        )
        return LabelledPage(
            page_id=page.page_id,
            file_id=page.file_id,
            seq=page.seq,
            file_page=page.file_page,
            slot=page.slot,
            image_key=page.image_key,
            doc_type=reply.doc_type,
            confidence=min(max(reply.confidence, 0.0), 1.0),
            quality_warning=page.quality_warning,
        )

    async def _store(
        self,
        claim_id: UUID,
        submission_id: UUID,
        pages: list[LabelledPage],
        *,
        created_by: str,
    ) -> DocumentSet:
        """Write document rows, page labels and the new stage record in one transaction."""
        async with self._sessions.begin() as session:
            required, listed = await self._checklist(session, claim_id, submission_id)
            documents = []
            for run in group_pages(pages):
                document = ClassifiedDocument(
                    document_id=uuid4(),
                    doc_type=run[0].doc_type,
                    page_range=PageRange(start=run[0].seq, end=run[-1].seq),
                    page_ids=[p.page_id for p in run],
                    confidence=min(p.confidence for p in run),
                )
                documents.append(document)
                session.add(
                    DocumentRow(
                        id=document.document_id,
                        submission_id=submission_id,
                        doc_type=document.doc_type.value,
                        page_start=document.page_range.start,
                        page_end=document.page_range.end,
                        confidence=document.confidence,
                    )
                )
            await session.flush()
            owner = {p: d for d in documents for p in d.page_ids}
            for page in pages:
                row = await session.get(PageRow, page.page_id)
                assert row is not None
                row.doc_type, row.confidence = page.doc_type.value, page.confidence
                row.document_id = owner[page.page_id].document_id
            doc_set = DocumentSet(
                claim_id=claim_id,
                submission_id=submission_id,
                pages=pages,
                documents=documents,
                mismatches=mismatches(pages, required, listed),
                needs_review=any(p.confidence < REVIEW_BELOW for p in pages),
            )
            await append_in_session(
                session,
                claim_id,
                Stage.CLASSIFICATION,
                doc_set.model_dump(mode="json"),
                created_by=created_by,
            )
            await self._record_missing(session, doc_set, created_by=created_by)
        return doc_set

    @staticmethod
    async def _record_missing(
        session: AsyncSession, doc_set: DocumentSet, *, created_by: str
    ) -> None:
        """For the claim's own submission with a merged file, intake could not list the
        missing documents; the classified pages do. Record them on the submission and as a
        new intake version, so the query and adjudication stages see them. Slotted uploads
        and follow-ups keep what intake found."""
        claim = await session.get(ClaimRow, doc_set.claim_id)
        submission = await session.get(SubmissionRow, doc_set.submission_id)
        assert claim is not None and submission is not None
        merged = await session.scalar(
            select(FileRow.id)
            .where(FileRow.submission_id == submission.id, FileRow.slot_hint.is_(None))
            .limit(1)
        )
        if claim.submission_id != submission.id or merged is None:
            return
        missing = [m.doc_type.value for m in doc_set.mismatches if m.kind == "missing"]
        if missing == submission.missing_documents:
            return
        submission.missing_documents = missing
        intake = await session.scalar(
            select(StageRecordRow)
            .where(StageRecordRow.claim_id == claim.id, StageRecordRow.stage == Stage.INTAKE.value)
            .order_by(StageRecordRow.version.desc())
            .limit(1)
        )
        if intake is not None:
            await append_in_session(
                session,
                claim.id,
                Stage.INTAKE,
                {**intake.payload, "missing_documents": missing},
                created_by=created_by,
            )

    @staticmethod
    async def _checklist(
        session: AsyncSession, claim_id: UUID, submission_id: UUID
    ) -> tuple[list[DocType], list[DocType]]:
        """The claim's checklist; query replies are checked against it too. A later
        cashless request (an enhancement, the discharge) has a checklist of its own."""
        claim = await session.get(ClaimRow, claim_id)
        submission = await session.get(SubmissionRow, submission_id)
        assert claim is not None and submission is not None
        amount = Decimal(str(submission.form.get("claimed_amount", "0")))
        request = RequestType(submission.request_type)
        if request not in (RequestType.ENHANCEMENT, RequestType.DISCHARGE):
            request = RequestType(claim.request_type)
        return await checklist(session, request, amount)
