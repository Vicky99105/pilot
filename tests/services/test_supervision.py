"""The supervisor service through its contract: the overview, hold and resume, and
reassignment, on SQLite with the real task service and fake monitoring and signaller."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow
from app.services.supervision.contract import (
    InvalidAction,
    NotSupervisor,
    SupervisionService,
)
from app.services.supervision.service import DefaultSupervisionService
from app.services.tasks.contract import TaskNotAllowed
from app.services.tasks.service import DefaultTaskService
from app.shared.types import Role, Stage
from tests.database.factories import add_claim, add_user
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.tasks import FakeWorkflowSignaller


class FakeClaimSignaller:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def hold(self, *, workflow_id: str) -> None:
        self.sent.append(("hold", workflow_id))

    async def resume(self, *, workflow_id: str) -> None:
        self.sent.append(("resume", workflow_id))


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.tasks = DefaultTaskService(sessions, FakeWorkflowSignaller())
        self.monitoring = FakeMonitoring()
        self.signaller = FakeClaimSignaller()
        self.service: SupervisionService = DefaultSupervisionService(
            sessions, self.tasks, self.monitoring, self.signaller
        )

    async def claim(self, status: str = "registration") -> UUID:
        claim_id = await add_claim(self.sessions, status=status)
        async with self.sessions.begin() as session:
            row = await session.get(ClaimRow, claim_id)
            assert row is not None
            row.workflow_id = str(claim_id)
            row.claim_number = f"CLM-2026-{str(claim_id)[:6]}"
        return claim_id

    async def task(self, claim_id: UUID, kind: str, role: Role, minutes: int) -> UUID:
        task = await self.tasks.open_task(
            claim_id=claim_id,
            workflow_id=str(claim_id),
            kind=kind,
            role=role,
            title=kind,
            payload={"stage": "registration"},
            deadline=datetime.now(UTC) + timedelta(minutes=minutes),
        )
        return task.id


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    return Env(sessions)


async def test_only_a_supervisor_sees_the_overview_or_acts(env: Env) -> None:
    ops = await add_user(env.sessions, "ops", [Role.CLAIMS_OPERATOR])
    claim_id = await env.claim()

    with pytest.raises(NotSupervisor):
        await env.service.overview(by=ops)
    with pytest.raises(NotSupervisor):
        await env.service.hold(claim_id, "tip-off", by=ops)


async def test_the_overview_shows_every_queue_tasks_near_breach_and_held_claims(
    env: Env,
) -> None:
    boss = await add_user(env.sessions, "boss", [Role.SUPERVISOR])
    first, second = await env.claim(), await env.claim()
    await env.task(first, "confirm_registration", Role.CLAIMS_OPERATOR, minutes=-5)
    await env.task(second, "approve", Role.APPROVER, minutes=30)
    await env.task(second, "confirm_deductions", Role.BILL_PROCESSOR, minutes=600)
    await env.service.hold(second, "Hospital under investigation", by=boss)

    overview = await env.service.overview(by=boss, near_breach_minutes=60)

    depth = {q.role: q for q in overview.queues}
    assert {Role.QC_AUDITOR, Role.INVESTIGATOR, Role.FINANCE} <= set(depth)
    assert (depth[Role.CLAIMS_OPERATOR].unfinished, depth[Role.CLAIMS_OPERATOR].overdue) == (1, 1)
    assert depth[Role.APPROVER].by_kind == {"approve": 1}
    assert depth[Role.QC_AUDITOR].unfinished == 0
    assert [(n.kind, n.held) for n in overview.near_breach] == [
        ("confirm_registration", False),
        ("approve", True),
    ]
    assert overview.near_breach[0].minutes_left < 0
    [held] = overview.held_claims
    assert (held.claim_id, held.reason, held.held_by) == (
        second,
        "Hospital under investigation",
        boss,
    )


async def test_throughput_and_override_rate_per_user(env: Env) -> None:
    boss = await add_user(env.sessions, "boss", [Role.SUPERVISOR])
    ops = await add_user(env.sessions, "ops", [Role.CLAIMS_OPERATOR, Role.BILL_PROCESSOR])
    claim_id = await env.claim()
    plain = await env.task(claim_id, "confirm_registration", Role.CLAIMS_OPERATOR, 60)
    await env.tasks.complete(task_id=plain, user_id=ops, outcome={"action": "confirm"})
    edit = {"line_id": 1, "admissible": "10", "reason": "x"}
    edited = await env.task(claim_id, "confirm_deductions", Role.BILL_PROCESSOR, 60)
    await env.tasks.complete(task_id=edited, user_id=ops, outcome={"lines": [edit]})
    other = await env.task(claim_id, "query_no_reply", Role.CLAIMS_OPERATOR, 60)
    await env.tasks.complete(task_id=other, user_id=ops, outcome={"action": "extend"})

    overview = await env.service.overview(by=boss)

    [activity] = overview.users
    assert (activity.user_id, activity.completed, activity.decided, activity.overridden) == (
        ops,
        3,
        2,
        1,
    )
    assert activity.override_rate == 0.5


async def test_hold_marks_the_claim_records_the_event_and_signals_the_workflow(
    env: Env,
) -> None:
    boss = await add_user(env.sessions, "boss", [Role.SUPERVISOR])
    claim_id = await env.claim()

    held = await env.service.hold(claim_id, "  Duplicate bill suspected  ", by=boss)

    assert held.reason == "Duplicate bill suspected"
    assert env.signaller.sent == [("hold", str(claim_id))]
    [event] = env.monitoring.recorded
    assert (event.action, event.stage, event.moves_status) == ("claim_held", Stage.MONITORING,
                                                               False)  # fmt: skip
    assert event.detail == {"reason": "Duplicate bill suspected"}
    assert event.actor == f"user:{boss}"
    with pytest.raises(InvalidAction, match="already"):
        await env.service.hold(claim_id, "again", by=boss)

    await env.service.resume(claim_id, "Bill verified with the hospital", by=boss)

    assert env.signaller.sent[-1] == ("resume", str(claim_id))
    assert env.monitoring.recorded[-1].action == "claim_resumed"
    async with env.sessions() as session:
        row = await session.get(ClaimRow, claim_id)
    assert row is not None and row.held_at is None and row.hold_reason is None
    with pytest.raises(InvalidAction, match="not held"):
        await env.service.resume(claim_id, "again", by=boss)


@pytest.mark.parametrize(
    ("status", "reason", "match"),
    [("closed", "x", "closed"), ("registration", "  ", "reason")],
    ids=["closed_claim", "no_reason"],
)
async def test_a_hold_needs_an_open_claim_and_a_reason(
    env: Env, status: str, reason: str, match: str
) -> None:
    boss = await add_user(env.sessions, "boss", [Role.SUPERVISOR])
    claim_id = await env.claim(status=status)

    with pytest.raises(InvalidAction, match=match):
        await env.service.hold(claim_id, reason, by=boss)
    with pytest.raises(InvalidAction, match="not found"):
        await env.service.hold(uuid4(), "x", by=boss)


async def test_reassignment_moves_the_task_and_records_who_and_why(env: Env) -> None:
    boss = await add_user(env.sessions, "boss", [Role.SUPERVISOR])
    first = await add_user(env.sessions, "first", [Role.APPROVER], max_amount=Decimal("1"))
    second = await add_user(env.sessions, "second", [Role.APPROVER])
    claim_id = await env.claim()
    task_id = await env.task(claim_id, "approve", Role.APPROVER, 60)

    moved = await env.service.reassign(task_id, second, "first is on leave", by=boss)

    assert (moved.assignee, moved.status) == (second, "taken")
    [event] = env.monitoring.recorded
    assert (event.action, event.moves_status, event.actor) == (
        "task_reassigned",
        False,
        f"user:{boss}",
    )
    assert event.detail == {
        "task_id": str(task_id),
        "kind": "approve",
        "to": str(second),
        "reason": "first is on leave",
    }
    with pytest.raises(NotSupervisor):
        await env.service.reassign(task_id, first, "x", by=first)
    with pytest.raises(InvalidAction, match="reason"):
        await env.service.reassign(task_id, second, " ", by=boss)
    del first
    with pytest.raises(TaskNotAllowed):
        await env.service.reassign(task_id, boss, "boss takes it", by=boss)
