"""The override log through its contract: what the correcting services wrote, newest first,
filtered by claim or by user."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.overrides import OverrideRepository
from app.services.overrides.contract import OverrideEntry
from app.services.overrides.service import DefaultOverrideLog
from app.shared.seed_ids import DOCTOR_USER_ID, OPS_USER_ID
from app.shared.types import Stage
from tests.database.factories import add_claim


def entry(claim_id: object, actor: str, field: str) -> OverrideEntry:
    return OverrideEntry(
        claim_id=claim_id,
        stage=Stage.BILL_REVIEW,
        kind="deduction_edit",
        field=field,
        before="100.00",
        after="50.00",
        reason="half",
        actor=actor,
    )


async def test_the_log_lists_newest_first_and_filters_by_claim_and_user(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first, second = await add_claim(sessions), await add_claim(sessions)
    repo = OverrideRepository(sessions)
    await repo.append([entry(first, f"user:{OPS_USER_ID}", "line 1 admissible")])
    await repo.append([entry(second, f"user:{DOCTOR_USER_ID}", "line 2 admissible")])
    log = DefaultOverrideLog(sessions)

    everything = await log.list()
    assert [o.field for o in everything] == ["line 2 admissible", "line 1 admissible"]
    assert [o.field for o in await log.list(claim_id=first)] == ["line 1 admissible"]
    assert [o.field for o in await log.list(user_id=DOCTOR_USER_ID)] == ["line 2 admissible"]
    assert await log.list(claim_id=uuid4()) == []
