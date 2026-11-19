"""P2-6 on real Postgres, after migrations: the override log's JSONB before and after
values, and the queue filter that hides tasks of a held claim and of another system of
medicine (an EXISTS subquery under FOR UPDATE SKIP LOCKED)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, TaskRow
from app.database.repositories.overrides import OverrideRepository
from app.database.seed import seed_claims_db
from app.services.overrides.contract import OverrideEntry
from app.services.overrides.service import DefaultOverrideLog
from app.services.tasks.service import DefaultTaskService
from app.shared.seed_ids import AYUSH_DOCTOR_USER_ID, DOCTOR_USER_ID, OPS_USER_ID
from app.shared.types import Role, Stage
from tests.database.factories import add_claim

pytestmark = pytest.mark.pg


class NoSignals:
    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None:
        return None


async def test_the_override_log_keeps_json_values_on_postgres(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim_id = await add_claim(pg_sessions)
    before = {"severity": "adverse", "text": "R5 failed"}
    await OverrideRepository(pg_sessions).append(
        [
            OverrideEntry(claim_id=claim_id, stage=Stage.MEDICAL_REVIEW, kind="medical_overrule",
                          field="finding 3", before=before, after="overruled",
                          reason="Ported", actor=f"user:{DOCTOR_USER_ID}"),
        ]
    )  # fmt: skip

    [logged] = await DefaultOverrideLog(pg_sessions).list(user_id=DOCTOR_USER_ID)

    assert (logged.before, logged.after, logged.claim_id) == (before, "overruled", claim_id)


async def test_held_claims_and_other_streams_are_left_out_of_take_next(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(pg_sessions)
    tasks = DefaultTaskService(pg_sessions, NoSignals())
    held, ayush = await add_claim(pg_sessions), await add_claim(pg_sessions)
    async with pg_sessions.begin() as session:
        row = await session.get(ClaimRow, held)
        assert row is not None
        row.held_at = datetime.now(UTC)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    await tasks.open_task(claim_id=held, workflow_id="wf-held", kind="confirm_registration",
                          role=Role.CLAIMS_OPERATOR, title="t", payload={},
                          deadline=deadline)  # fmt: skip
    medical = await tasks.open_task(claim_id=ayush, workflow_id="wf-med", kind="medical_findings",
                                    role=Role.MEDICAL_OFFICER, title="t", payload={},
                                    deadline=deadline)  # fmt: skip
    async with pg_sessions.begin() as session:  # pretend the claim's hospital is AYUSH
        task_row = await session.get(TaskRow, medical.id)
        assert task_row is not None
        task_row.system_of_medicine = "ayush"

    assert await tasks.take_next(user_id=OPS_USER_ID) is None
    assert await tasks.take_next(user_id=DOCTOR_USER_ID) is None
    taken = await tasks.take_next(user_id=AYUSH_DOCTOR_USER_ID)
    assert taken is not None and taken.id == medical.id
