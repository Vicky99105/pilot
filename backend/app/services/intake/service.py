"""Default intake: check and store files, detect duplicates, list missing documents, create
the claim and start its workflow. The Temporal WorkflowStarter lives here too."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.config import Settings
from app.database.models import ClaimRow, FileRow, SubmissionRow, utcnow
from app.database.repositories.document_checklists import checklist
from app.database.repositories.stage_records import append_in_session
from app.services.file_store.contract import FileStore
from app.services.intake._helpers import CheckedFile, check_file, files_digest
from app.services.intake.contract import (
    ClaimForm,
    Submission,
    SubmissionInput,
    SubmissionRejected,
    SubmittedFile,
    WorkflowAlreadyStarted,
    WorkflowStarter,
)
from app.shared.types import DocType, RequestType, Stage
from app.workflows.claim_models import ClaimInput

logger = logging.getLogger(__name__)

RECEIVED = "received"
TURNAROUND = {
    RequestType.PREAUTH: timedelta(hours=1),
    RequestType.ENHANCEMENT: timedelta(hours=1),
    RequestType.DISCHARGE: timedelta(hours=3),
}
DEFAULT_TURNAROUND = timedelta(days=15)  # reimbursement and every other request type
MERGED_WARNING = "Merged upload: missing documents are checked after classification"
NOT_STARTED_WARNING = "Processing not started yet (workflow service unavailable); resubmit to retry"


class DefaultIntakeService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        file_store: FileStore,
        starter: WorkflowStarter,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._files = file_store
        self._starter = starter
        self._now = now

    async def accept(self, submission: SubmissionInput) -> Submission:
        checked = await self._check(submission)
        digest = files_digest([f.sha256 for f in checked])
        if submission.parent_claim_id is None:
            earlier = await self._find_duplicate(submission.form, digest)
            if earlier is not None:
                return await self._link_duplicate(submission, digest, earlier)
        elif not await self._claim_exists(submission.parent_claim_id):
            raise SubmissionRejected([f"unknown parent claim {submission.parent_claim_id}"])

        keys = [await self._files.put(f.data, content_type=f.mime) for f in checked]
        submission_id = await self._store(submission, checked, keys, digest)
        stored = await self._get_existing(submission_id)
        if submission.parent_claim_id is None:
            await self._start(stored)
            stored = await self._get_existing(submission_id)
        return stored

    async def get(self, submission_id: UUID) -> Submission | None:
        async with self._sessions() as session:
            return await _load(session, submission_id)

    async def _get_existing(self, submission_id: UUID) -> Submission:
        found = await self.get(submission_id)
        assert found is not None
        return found

    async def _check(self, submission: SubmissionInput) -> list[CheckedFile]:
        if not submission.files:
            raise SubmissionRejected(["no files attached"])
        results = [
            await asyncio.to_thread(check_file, f, submission.channel) for f in submission.files
        ]
        reasons = [r for r in results if isinstance(r, str)]
        if reasons:
            raise SubmissionRejected(reasons)
        return [r for r in results if isinstance(r, CheckedFile)]

    async def _find_duplicate(self, form: ClaimForm, digest: str) -> SubmissionRow | None:
        async with self._sessions() as session:
            row: SubmissionRow | None = await session.scalar(
                select(SubmissionRow)
                .where(
                    SubmissionRow.member_id == form.member_id,
                    SubmissionRow.admission_date == form.admission_date,
                    SubmissionRow.files_digest == digest,
                    SubmissionRow.duplicate_of.is_(None),
                    SubmissionRow.parent_claim_id.is_(None),
                )
                .order_by(SubmissionRow.created_at)
                .limit(1)
            )
            return row

    async def _claim_exists(self, claim_id: UUID) -> bool:
        async with self._sessions() as session:
            return await session.get(ClaimRow, claim_id) is not None

    async def _link_duplicate(
        self, submission: SubmissionInput, digest: str, earlier: SubmissionRow
    ) -> Submission:
        """Record the repeat for audit and point it at the earlier claim. No files are
        stored again and no claim is created."""
        row = self._submission_row(submission, digest)
        row.claim_id, row.duplicate_of = earlier.claim_id, earlier.id
        row.warnings = [f"Same documents as submission {earlier.id}; linked to its claim"]
        async with self._sessions.begin() as session:
            session.add(row)
        linked = await self._get_existing(row.id)
        if linked.workflow_id is None:  # the first start failed; this is the retry
            await self._start(linked)
            linked = await self._get_existing(row.id)
        return linked

    async def _store(
        self,
        submission: SubmissionInput,
        checked: list[CheckedFile],
        keys: list[str],
        digest: str,
    ) -> UUID:
        form = submission.form
        warnings = [f.warning for f in checked if f.warning]
        async with self._sessions.begin() as session:
            missing = await self._missing(session, submission, checked, warnings)
            row = self._submission_row(submission, digest)
            row.missing_documents = [d.value for d in missing]
            row.warnings = warnings
            session.add(row)
            await session.flush()
            if submission.parent_claim_id is not None:
                row.claim_id = submission.parent_claim_id
            else:
                claim = ClaimRow(
                    id=uuid4(),
                    submission_id=row.id,
                    member_id=form.member_id,
                    policy_number=form.policy_number,
                    hospital_id=form.hospital_id,
                    request_type=submission.request_type.value,
                    status=RECEIVED,
                    status_changed_at=row.created_at,
                    deadline=row.created_at
                    + TURNAROUND.get(submission.request_type, DEFAULT_TURNAROUND),
                    created_at=row.created_at,
                )
                session.add(claim)
                await session.flush()
                row.claim_id = claim.id
            session.add_all(
                FileRow(
                    id=uuid4(),
                    submission_id=row.id,
                    position=i,
                    filename=f.filename,
                    sha256=f.sha256,
                    slot_hint=f.slot.value if f.slot else None,
                    mime=f.mime,
                    size_bytes=f.size_bytes,
                    storage_key=key,
                    created_at=row.created_at,
                )
                for i, (f, key) in enumerate(zip(checked, keys, strict=True))
            )
            await session.flush()
            if submission.parent_claim_id is None:
                record = await _load(session, row.id)
                assert record is not None
                await append_in_session(
                    session,
                    row.claim_id,
                    Stage.INTAKE,
                    record.model_dump(mode="json"),
                    created_by=f"user:{submission.submitted_by}",
                )
            return row.id

    def _submission_row(self, submission: SubmissionInput, digest: str) -> SubmissionRow:
        return SubmissionRow(
            id=uuid4(),
            request_type=submission.request_type.value,
            submitted_by=submission.submitted_by,
            parent_claim_id=submission.parent_claim_id,
            channel=submission.channel,
            form=submission.form.model_dump(mode="json"),
            member_id=submission.form.member_id,
            admission_date=submission.form.admission_date,
            files_digest=digest,
            missing_documents=[],
            warnings=[],
            created_at=self._now(),
        )

    @staticmethod
    async def _missing(
        session: AsyncSession,
        submission: SubmissionInput,
        checked: list[CheckedFile],
        warnings: list[str],
    ) -> list[DocType]:
        """Required documents with no file in their slot. A merged upload has no slots, so
        classification checks it instead. Missing documents never block a submission."""
        if any(f.slot is None for f in checked):
            warnings.append(MERGED_WARNING)
            return []
        required, _ = await checklist(
            session, submission.request_type, submission.form.claimed_amount
        )
        slots = {f.slot for f in checked}
        return [d for d in required if d not in slots]

    async def _start(self, submission: Submission) -> None:
        workflow_id = str(submission.claim_id)
        try:
            await self._starter.start_claim(
                workflow_id=workflow_id,
                submission_id=submission.submission_id,
                request_type=submission.request_type,
            )
        except WorkflowAlreadyStarted:
            pass  # started by an earlier attempt; record the ID below
        except Exception:
            logger.warning("Could not start workflow %s", workflow_id, exc_info=True)
            async with self._sessions.begin() as session:
                row = await session.get(SubmissionRow, submission.submission_id)
                assert row is not None
                row.warnings = [*row.warnings, NOT_STARTED_WARNING]
            return
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, submission.claim_id)
            assert claim is not None
            claim.workflow_id = workflow_id


async def _load(session: AsyncSession, submission_id: UUID) -> Submission | None:
    await session.flush()
    row = await session.get(SubmissionRow, submission_id)
    if row is None or row.claim_id is None:
        return None
    claim = await session.get(ClaimRow, row.claim_id)
    assert claim is not None
    files = await session.scalars(
        select(FileRow).where(FileRow.submission_id == row.id).order_by(FileRow.position)
    )
    return Submission(
        submission_id=row.id,
        claim_id=claim.id,
        request_type=RequestType(row.request_type),
        parent_claim_id=row.parent_claim_id,
        form=ClaimForm.model_validate(row.form),
        files=[
            SubmittedFile(
                file_id=f.id,
                filename=f.filename,
                sha256=f.sha256,
                slot=DocType(f.slot_hint) if f.slot_hint else None,
                mime=f.mime,
                size_bytes=f.size_bytes,
                storage_key=f.storage_key,
                quality_warning=f.quality_warning,
            )
            for f in files
        ],
        missing_documents=[DocType(d) for d in row.missing_documents],
        warnings=row.warnings,
        deadline=claim.deadline,
        duplicate_of=row.duplicate_of,
        workflow_id=claim.workflow_id,
        created_at=row.created_at,
    )


class DefaultWorkflowStarter:
    """Starts ClaimWorkflow on Temporal. The workflow ID is the claim ID; a second start
    with the same ID is refused, even after the first workflow has closed."""

    def __init__(self, settings: Settings, client: Client | None = None) -> None:
        self._settings = settings
        self._client = client

    async def start_claim(
        self, *, workflow_id: str, submission_id: UUID, request_type: RequestType
    ) -> None:
        client = await self._get_client()
        try:
            await client.start_workflow(
                "ClaimWorkflow",
                ClaimInput(submission_id=submission_id, request_type=request_type),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError as exc:
            raise WorkflowAlreadyStarted(workflow_id) from exc

    async def _get_client(self) -> Client:
        if self._client is None:
            self._client = await Client.connect(
                self._settings.temporal_address,
                namespace=self._settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            )
        return self._client
