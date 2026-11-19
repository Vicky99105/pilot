"""Versioned stage outputs. A write always appends a new version; nothing is updated."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import StageRecordRow
from app.shared.types import Stage


class StageRecord(BaseModel):
    id: UUID
    claim_id: UUID
    stage: Stage
    version: int
    payload: dict[str, Any]
    created_at: datetime
    created_by: str


def _to_record(row: StageRecordRow) -> StageRecord:
    return StageRecord(
        id=row.id,
        claim_id=row.claim_id,
        stage=Stage(row.stage),
        version=row.version,
        payload=row.payload,
        created_at=row.created_at,
        created_by=row.created_by,
    )


async def append_in_session(
    session: AsyncSession,
    claim_id: UUID,
    stage: Stage,
    payload: dict[str, Any],
    *,
    created_by: str,
) -> StageRecord:
    """Write the next version inside the caller's transaction. A concurrent writer for the
    same stage fails on the (claim_id, stage, version) unique constraint instead of
    overwriting."""
    current = await session.scalar(
        select(func.max(StageRecordRow.version)).where(
            StageRecordRow.claim_id == claim_id, StageRecordRow.stage == stage.value
        )
    )
    row = StageRecordRow(
        id=uuid4(),
        claim_id=claim_id,
        stage=stage.value,
        version=(current or 0) + 1,
        payload=payload,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _to_record(row)


class StageRecordRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(
        self, claim_id: UUID, stage: Stage, payload: dict[str, Any], *, created_by: str
    ) -> StageRecord:
        async with self._sessions.begin() as session:
            return await append_in_session(session, claim_id, stage, payload, created_by=created_by)

    async def latest(self, claim_id: UUID, stage: Stage) -> StageRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(StageRecordRow)
                .where(StageRecordRow.claim_id == claim_id, StageRecordRow.stage == stage.value)
                .order_by(StageRecordRow.version.desc())
                .limit(1)
            )
            return _to_record(row) if row else None

    async def history(self, claim_id: UUID, stage: Stage) -> list[StageRecord]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(StageRecordRow)
                .where(StageRecordRow.claim_id == claim_id, StageRecordRow.stage == stage.value)
                .order_by(StageRecordRow.version)
            )
            return [_to_record(r) for r in rows]

    async def latest_per_stage(self, claim_id: UUID) -> dict[Stage, StageRecord]:
        newest = (
            select(StageRecordRow.stage, func.max(StageRecordRow.version).label("version"))
            .where(StageRecordRow.claim_id == claim_id)
            .group_by(StageRecordRow.stage)
            .subquery()
        )
        async with self._sessions() as session:
            rows = await session.scalars(
                select(StageRecordRow)
                .join(
                    newest,
                    (StageRecordRow.stage == newest.c.stage)
                    & (StageRecordRow.version == newest.c.version),
                )
                .where(StageRecordRow.claim_id == claim_id)
            )
            return {Stage(r.stage): _to_record(r) for r in rows}
