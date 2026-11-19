"""take_next on real Postgres: FOR UPDATE SKIP LOCKED gives concurrent callers different
tasks, and a locked task is skipped rather than waited for."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import TaskRow
from app.database.seed import seed_claims_db
from app.services.tasks.service import DefaultTaskService
from app.shared.seed_ids import OPS_USER_ID
from app.shared.types import Role
from tests.database.factories import add_claim

pytestmark = pytest.mark.pg

TASKS = 10


class NoSignals:
    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None:
        return None


async def _open_tasks(sessions: async_sessionmaker[AsyncSession]) -> DefaultTaskService:
    await seed_claims_db(sessions)
    service = DefaultTaskService(sessions, NoSignals())
    start = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    for i in range(TASKS):
        claim_id = await add_claim(sessions)
        await service.open_task(
            claim_id=claim_id,
            workflow_id=str(uuid4()),
            kind="fix_fields",
            role=Role.CLAIMS_OPERATOR,
            title=f"task {i}",
            payload={},
            deadline=start + timedelta(minutes=i),
        )
    return service


async def test_concurrent_callers_each_take_a_different_task(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    service = await _open_tasks(pg_sessions)

    taken = await asyncio.gather(*(service.take_next(user_id=OPS_USER_ID) for _ in range(TASKS)))

    ids = [t.id for t in taken if t is not None]
    assert len(ids) == TASKS and len(set(ids)) == TASKS
    assert await service.take_next(user_id=OPS_USER_ID) is None


async def test_a_task_locked_by_another_transaction_is_skipped_not_waited_for(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    service = await _open_tasks(pg_sessions)
    async with pg_sessions.begin() as other:
        first = await other.scalar(
            select(TaskRow).order_by(TaskRow.deadline).limit(1).with_for_update()
        )
        assert first is not None

        task = await asyncio.wait_for(service.take_next(user_id=OPS_USER_ID), timeout=5)

        assert task is not None and task.id != first.id
        assert task.title == "task 1"  # the next most urgent
