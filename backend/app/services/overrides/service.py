"""Default override log: reads the overrides table, newest first."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, OverrideRow
from app.services.overrides.contract import Override
from app.shared.types import Stage


class DefaultOverrideLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(
        self, *, claim_id: UUID | None = None, user_id: UUID | None = None, limit: int = 200
    ) -> list[Override]:
        query = (
            select(OverrideRow, ClaimRow.claim_number)
            .outerjoin(ClaimRow, ClaimRow.id == OverrideRow.claim_id)
            .order_by(OverrideRow.created_at.desc(), OverrideRow.field)
            .limit(limit)
        )
        if claim_id is not None:
            query = query.where(OverrideRow.claim_id == claim_id)
        if user_id is not None:
            query = query.where(OverrideRow.user_id == user_id)
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        return [
            Override(
                override_id=row.id,
                claim_id=row.claim_id,
                claim_number=number,
                stage=Stage(row.stage),
                kind=row.kind,
                field=row.field,
                before=row.before,
                after=row.after,
                reason=row.reason,
                actor=row.actor,
                user_id=row.user_id,
                created_at=row.created_at,
            )
            for row, number in rows
        ]
