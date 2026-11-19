"""Default reconsideration: checks the claim is closed, records what it was, reopens it and
hands the new evidence to intake, which starts the run that decides it again."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, ReconsiderationRow, utcnow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.intake.contract import (
    Channel,
    IntakeService,
    SubmissionRejected,
    UploadedFile,
)
from app.services.monitoring.contract import Monitoring
from app.services.reconsideration.contract import (
    MIN_GROUNDS,
    NotReconsiderable,
    Reconsideration,
)
from app.shared.types import RequestType, Stage

CLOSED = "closed"
REOPENED = "reconsideration"  # the claim's status until its run records a stage


class DefaultReconsiderationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        intake: IntakeService,
        monitoring: Monitoring,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._intake = intake
        self._monitoring = monitoring
        self._records = StageRecordRepository(sessions)
        self._now = now

    async def request(
        self,
        claim_id: UUID,
        *,
        grounds: str,
        submitted_by: UUID,
        channel: Channel,
        files: list[UploadedFile],
    ) -> Reconsideration:
        grounds = grounds.strip()
        if len(grounds) < MIN_GROUNDS:
            raise NotReconsiderable(f"the grounds need at least {MIN_GROUNDS} characters")
        if not files:
            raise NotReconsiderable("a reconsideration needs new evidence: at least one file")
        decision = await self._records.latest(claim_id, Stage.DECISION)
        paid = await self._records.settled_paid(claim_id)
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is None:
                raise NotReconsiderable(f"claim {claim_id} not found")
            if claim.status != CLOSED:
                raise NotReconsiderable(f"claim {claim.claim_number or claim_id} is not closed")
            closed_at = claim.closed_at
            number = 1 + int(
                await session.scalar(
                    select(func.count(ReconsiderationRow.id)).where(
                        ReconsiderationRow.claim_id == claim_id
                    )
                )
                or 0
            )
            # Reopen first, so the run's first events move the status again.
            claim.status, claim.status_changed_at, claim.closed_at = REOPENED, self._now(), None
        try:
            submission = await self._intake.accept_followup(
                claim_id,
                submitted_by=submitted_by,
                channel=channel,
                files=files,
                request_type=RequestType.RECONSIDERATION,
            )
        except SubmissionRejected as exc:
            await self._close_again(claim_id, closed_at)
            raise NotReconsiderable(f"the evidence was refused: {exc}") from exc
        payload = (decision.payload if decision else None) or {}
        row = ReconsiderationRow(
            id=uuid4(),
            claim_id=claim_id,
            number=number,
            submission_id=submission.submission_id,
            workflow_id=submission.workflow_id or "",
            grounds=grounds,
            requested_by=submitted_by,
            requested_at=self._now(),
            previous_decision=payload.get("final_decision"),
            previous_amount=Decimal(str(payload.get("final_amount") or "0")),
            previously_paid=paid,
            previous_closed_at=closed_at,
        )
        async with self._sessions.begin() as session:
            session.add(row)
        await self._monitoring.record_event(
            claim_id=claim_id,
            stage=Stage.INTAKE,
            action="reconsideration_requested",
            actor=f"user:{submitted_by}",
            detail={
                "number": number,
                "grounds": grounds,
                "submission_id": str(submission.submission_id),
                "workflow_id": row.workflow_id,
                "previous_decision": row.previous_decision,
                "previously_paid": str(paid),
            },
        )
        return _to_model(row)

    async def history(self, claim_id: UUID) -> list[Reconsideration]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ReconsiderationRow)
                .where(ReconsiderationRow.claim_id == claim_id)
                .order_by(ReconsiderationRow.number)
            )
            return [_to_model(r) for r in rows]

    async def _close_again(self, claim_id: UUID, closed_at: datetime | None) -> None:
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            assert claim is not None
            claim.status, claim.closed_at = CLOSED, closed_at


def _to_model(row: ReconsiderationRow) -> Reconsideration:
    return Reconsideration(
        reconsideration_id=row.id,
        claim_id=row.claim_id,
        number=row.number,
        submission_id=row.submission_id,
        workflow_id=row.workflow_id,
        grounds=row.grounds,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        previous_decision=row.previous_decision,
        previous_amount=row.previous_amount,
        previously_paid=row.previously_paid,
        previous_closed_at=row.previous_closed_at,
    )
