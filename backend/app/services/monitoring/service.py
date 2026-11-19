"""Default Monitoring: appends to events and keeps claims.status at the current stage."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, EventRow, utcnow
from app.services.monitoring.contract import ClaimStatusRow
from app.shared.types import Stage


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
            if claim is not None and claim.status != stage.value:
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
