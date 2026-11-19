from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.stage_records import StageRecordRepository
from app.shared.types import Stage
from tests.database.factories import add_claim


async def test_append_writes_version_1_then_2_and_latest_returns_newest(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim_id = await add_claim(sessions)
    repo = StageRecordRepository(sessions)

    first = await repo.append(claim_id, Stage.BILL_REVIEW, {"payable": "100.00"}, created_by="ops")
    second = await repo.append(claim_id, Stage.BILL_REVIEW, {"payable": "80.00"}, created_by="ops")
    latest = await repo.latest(claim_id, Stage.BILL_REVIEW)

    assert (first.version, second.version) == (1, 2)
    assert latest is not None
    assert latest.version == 2
    assert latest.payload == {"payable": "80.00"}
    assert latest.created_at.tzinfo is not None


async def test_old_versions_are_kept(sessions: async_sessionmaker[AsyncSession]) -> None:
    claim_id = await add_claim(sessions)
    repo = StageRecordRepository(sessions)
    await repo.append(claim_id, Stage.QUERY, {"status": "open"}, created_by="system")
    await repo.append(claim_id, Stage.QUERY, {"status": "answered"}, created_by="system")

    history = await repo.history(claim_id, Stage.QUERY)

    assert [r.payload["status"] for r in history] == ["open", "answered"]


async def test_latest_per_stage_returns_newest_version_of_each_stage(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim_id = await add_claim(sessions)
    other_claim = await add_claim(sessions)
    repo = StageRecordRepository(sessions)
    await repo.append(claim_id, Stage.REGISTRATION, {"n": 1}, created_by="system")
    await repo.append(claim_id, Stage.REGISTRATION, {"n": 2}, created_by="system")
    await repo.append(claim_id, Stage.RISK, {"n": 1}, created_by="system")
    await repo.append(other_claim, Stage.RISK, {"n": 9}, created_by="system")

    latest = await repo.latest_per_stage(claim_id)

    assert {stage: r.payload["n"] for stage, r in latest.items()} == {
        Stage.REGISTRATION: 2,
        Stage.RISK: 1,
    }


async def test_latest_returns_none_when_stage_has_no_record(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repo = StageRecordRepository(sessions)

    assert await repo.latest(uuid4(), Stage.INTAKE) is None
