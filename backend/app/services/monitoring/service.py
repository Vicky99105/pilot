"""Default Monitoring: appends to events and keeps claims.status at the current stage. A
closed claim keeps the status "closed"; later events are still recorded."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, EventRow, utcnow
from app.services.monitoring.contract import ClaimEvent, ClaimStatusRow, ClaimTurnaround
from app.shared.types import Stage

CLOSED = "closed"


class DefaultMonitoring:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._now = now

    async def record_event(
        self,
        *,
        claim_id: UUID,
        stage: Stage,
        action: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = self._now()
        async with self._sessions.begin() as session:
            session.add(
                EventRow(
                    id=uuid4(),
                    claim_id=claim_id,
                    stage=stage.value,
                    action=action,
                    actor=actor,
                    detail=detail,
                    created_at=now,
                )
            )
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is not None and claim.status not in (stage.value, CLOSED):
                claim.status, claim.status_changed_at = stage.value, now

    async def claims_by_stage(self) -> list[ClaimStatusRow]:
        now = self._now()
        async with self._sessions() as session:
            claims = await session.scalars(select(ClaimRow).order_by(ClaimRow.status_changed_at))
            return [
                ClaimStatusRow(
                    claim_id=c.id,
                    claim_number=c.claim_number,
                    state=c.status,
                    entered_at=c.status_changed_at,
                    dwell_seconds=max(0, int((now - c.status_changed_at).total_seconds())),
                    deadline=c.deadline,
                )
                for c in claims
            ]

    async def events(self, claim_id: UUID) -> list[ClaimEvent]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(EventRow)
                .where(EventRow.claim_id == claim_id)
                .order_by(EventRow.created_at, EventRow.id)
            )
            return [
                ClaimEvent(
                    stage=Stage(r.stage),
                    action=r.action,
                    actor=r.actor,
                    detail=r.detail,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def summarise(self, claim_id: UUID) -> ClaimTurnaround:
        events = await self.events(claim_id)
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
        if claim is None:
            raise LookupError(f"claim {claim_id} not found")
        received = events[0].created_at if events else claim.created_at
        end = claim.closed_at or self._now()
        return ClaimTurnaround(
            claim_id=claim_id,
            status=claim.status,
            received_at=received,
            closed_at=claim.closed_at,
            turnaround_seconds=max(0, int((end - received).total_seconds())),
            deadline=claim.deadline,
            within_deadline=end <= claim.deadline if claim.deadline else None,
            checkpoints=sum(1 for e in events if e.action == "checkpoint_opened"),
        )
