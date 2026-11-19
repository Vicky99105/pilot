from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, EventRow
from app.services.monitoring.contract import Monitoring
from app.services.monitoring.service import DefaultMonitoring
from app.shared.types import Stage
from tests.database.factories import add_claim


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _events(sessions: async_sessionmaker[AsyncSession]) -> list[EventRow]:
    async with sessions() as session:
        return list((await session.scalars(select(EventRow).order_by(EventRow.created_at))).all())


async def test_record_event_appends_event_and_moves_claim_to_the_stage(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    monitoring: Monitoring = DefaultMonitoring(sessions, now=clock)
    claim_id = await add_claim(sessions)

    await monitoring.record_event(
        claim_id=claim_id, stage=Stage.REGISTRATION, action="completed", actor="system"
    )
    clock.now += timedelta(minutes=30)
    rows = await monitoring.claims_by_stage()

    [row] = rows
    assert (row.claim_id, row.state) == (claim_id, "registration")
    assert row.entered_at == datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    assert row.dwell_seconds == 1800
    [event] = await _events(sessions)
    assert (event.stage, event.action, event.actor) == ("registration", "completed", "system")


async def test_events_in_the_same_stage_keep_the_entry_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    monitoring = DefaultMonitoring(sessions, now=clock)
    claim_id = await add_claim(sessions)
    await monitoring.record_event(
        claim_id=claim_id, stage=Stage.BILL_REVIEW, action="started", actor="system"
    )
    clock.now += timedelta(hours=2)
    await monitoring.record_event(
        claim_id=claim_id,
        stage=Stage.BILL_REVIEW,
        action="checkpoint_escalated",
        actor="system",
        detail={"task_id": "t-1"},
    )

    [row] = await monitoring.claims_by_stage()

    assert row.entered_at == datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
    assert [e.detail for e in await _events(sessions)] == [None, {"task_id": "t-1"}]


async def test_event_for_a_claim_without_a_row_is_still_recorded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    monitoring = DefaultMonitoring(sessions)

    await monitoring.record_event(claim_id=uuid4(), stage=Stage.INTAKE, action="x", actor="y")

    assert len(await _events(sessions)) == 1
    assert await monitoring.claims_by_stage() == []


async def test_events_lists_one_claims_history_in_order(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    monitoring = DefaultMonitoring(sessions, now=clock)
    claim_id, other = await add_claim(sessions), await add_claim(sessions)
    for stage, action in [(Stage.INTAKE, "stage_completed"), (Stage.REGISTRATION, "opened")]:
        await monitoring.record_event(
            claim_id=claim_id, stage=stage, action=action, actor="system", detail={"n": 1}
        )
        await monitoring.record_event(claim_id=other, stage=stage, action="x", actor="system")
        clock.now += timedelta(seconds=5)

    events = await monitoring.events(claim_id)

    assert [(e.stage, e.action, e.detail) for e in events] == [
        (Stage.INTAKE, "stage_completed", {"n": 1}),
        (Stage.REGISTRATION, "opened", {"n": 1}),
    ]
    assert events[1].created_at - events[0].created_at == timedelta(seconds=5)


async def test_a_closed_claim_stays_closed_and_the_summary_has_its_turnaround(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    monitoring = DefaultMonitoring(sessions, now=clock)
    claim_id = await add_claim(sessions)
    await monitoring.record_event(
        claim_id=claim_id, stage=Stage.INTAKE, action="stage_completed", actor="system"
    )
    async with sessions.begin() as session:
        claim = await session.get(ClaimRow, claim_id)
        assert claim is not None
        claim.status, claim.closed_at = "closed", clock.now + timedelta(hours=3)
        claim.deadline = clock.now + timedelta(days=15)
    clock.now += timedelta(hours=3)

    summary = await monitoring.summarise(claim_id)
    await monitoring.record_event(
        claim_id=claim_id, stage=Stage.MONITORING, action="stage_completed", actor="system"
    )

    assert (summary.status, summary.turnaround_seconds, summary.within_deadline) == (
        "closed",
        3 * 3600,
        True,
    )
    [row] = await monitoring.claims_by_stage()
    assert row.state == "closed"
