from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.tasks.contract import (
    Task,
    TaskAlreadyDone,
    TaskNotAllowed,
    TaskNotFound,
    TaskService,
)
from app.services.tasks.service import DefaultTaskService
from app.shared.types import Role
from tests.database.factories import add_claim, add_user
from tests.fakes.tasks import FakeWorkflowSignaller

NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)


class Ctx:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.signaller = FakeWorkflowSignaller()
        self.service: TaskService = DefaultTaskService(sessions, self.signaller)
        self.claim_id: UUID = uuid4()

    async def open(
        self,
        kind: str,
        role: Role,
        hours: int,
        workflow_id: str = "wf-1",
        authority_amount: Decimal | None = None,
    ) -> Task:
        return await self.service.open_task(
            claim_id=self.claim_id,
            workflow_id=workflow_id,
            kind=kind,
            role=role,
            title=f"{kind} title",
            payload={"kind": kind},
            deadline=NOW + timedelta(hours=hours),
            authority_amount=authority_amount,
        )


@pytest.fixture
async def ctx(sessions: async_sessionmaker[AsyncSession]) -> Ctx:
    c = Ctx(sessions)
    c.claim_id = await add_claim(sessions)
    return c


async def test_queue_holds_only_tasks_for_the_users_roles_ordered_by_deadline(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR, Role.BILL_PROCESSOR])
    await ctx.open("confirm_deductions", Role.BILL_PROCESSOR, hours=8)
    await ctx.open("approve", Role.APPROVER, hours=1)
    await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=2, workflow_id="wf-2")

    queue = await ctx.service.list_queue(user_id=ops)

    assert [t.kind for t in queue] == ["confirm_registration", "confirm_deductions"]


async def test_queue_hides_done_tasks_and_tasks_taken_by_others(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    other = await add_user(ctx.sessions, "other", [Role.CLAIMS_OPERATOR])
    await ctx.open("a", Role.CLAIMS_OPERATOR, hours=1, workflow_id="wf-a")
    await ctx.open("b", Role.CLAIMS_OPERATOR, hours=2, workflow_id="wf-b")
    await ctx.open("c", Role.CLAIMS_OPERATOR, hours=3, workflow_id="wf-c")
    taken_by_other = await ctx.service.take_next(user_id=other)
    assert taken_by_other is not None
    mine = await ctx.service.take_next(user_id=ops)
    assert mine is not None
    await ctx.service.complete(task_id=mine.id, user_id=ops, outcome={"action": "confirm"})

    queue = await ctx.service.list_queue(user_id=ops)

    assert [t.kind for t in queue] == ["c"]


async def test_take_next_locks_the_most_urgent_allowed_task(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    ops2 = await add_user(ctx.sessions, "ops2", [Role.CLAIMS_OPERATOR])
    await ctx.open("later", Role.CLAIMS_OPERATOR, hours=5, workflow_id="wf-x")
    await ctx.open("sooner", Role.CLAIMS_OPERATOR, hours=1, workflow_id="wf-y")
    await ctx.open("not mine", Role.FINANCE, hours=0, workflow_id="wf-z")

    first = await ctx.service.take_next(user_id=ops)
    second = await ctx.service.take_next(user_id=ops2)
    none_left = await ctx.service.take_next(user_id=ops2)

    assert first is not None and second is not None
    assert (first.kind, first.status, first.assignee) == ("sooner", "taken", ops)
    assert (second.kind, second.assignee) == ("later", ops2)
    assert none_left is None


async def test_complete_persists_then_signals_the_workflow(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    await ctx.service.take_next(user_id=ops)

    done = await ctx.service.complete(task_id=task.id, user_id=ops, outcome={"action": "confirm"})

    assert (done.status, done.outcome) == ("done", {"action": "confirm"})
    stored = await ctx.service.get_task(task_id=task.id, user_id=ops)
    assert stored is not None and stored.status == "done"
    # The signal names who completed the task, so the workflow can record the person.
    assert ctx.signaller.signals == [
        ("wf-1", task.id, {"action": "confirm", "completed_by": str(ops)})
    ]


async def test_complete_open_task_by_role_holder_is_allowed(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)

    done = await ctx.service.complete(task_id=task.id, user_id=ops, outcome={})

    assert (done.status, done.assignee) == ("done", ops)


async def test_complete_is_refused_without_the_role_or_when_taken_by_another(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    ops2 = await add_user(ctx.sessions, "ops2", [Role.CLAIMS_OPERATOR])
    finance = await add_user(ctx.sessions, "finance", [Role.FINANCE])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    await ctx.service.take_next(user_id=ops)

    with pytest.raises(TaskNotAllowed):
        await ctx.service.complete(task_id=task.id, user_id=finance, outcome={})
    with pytest.raises(TaskNotAllowed):
        await ctx.service.complete(task_id=task.id, user_id=ops2, outcome={})
    assert ctx.signaller.signals == []


async def test_complete_twice_or_unknown_task_raises(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    await ctx.service.complete(task_id=task.id, user_id=ops, outcome={})

    with pytest.raises(TaskAlreadyDone):
        await ctx.service.complete(task_id=task.id, user_id=ops, outcome={})
    with pytest.raises(TaskNotFound):
        await ctx.service.complete(task_id=uuid4(), user_id=ops, outcome={})
    assert len(ctx.signaller.signals) == 1


async def test_open_task_is_idempotent_while_the_same_checkpoint_is_unfinished(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    first = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    retry = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    await ctx.service.complete(task_id=first.id, user_id=ops, outcome={"action": "send_back"})
    rerun = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)

    assert retry.id == first.id
    assert rerun.id != first.id


async def test_get_task_is_hidden_from_users_without_the_role(ctx: Ctx) -> None:
    finance = await add_user(ctx.sessions, "finance", [Role.FINANCE])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)

    assert await ctx.service.get_task(task_id=task.id, user_id=finance) is None


async def test_an_approver_task_above_the_users_limit_is_hidden_and_refused(ctx: Ctx) -> None:
    doctor = await add_user(ctx.sessions, "doctor", [Role.APPROVER], max_amount=Decimal("100000"))
    senior = await add_user(ctx.sessions, "senior", [Role.APPROVER])  # no limit
    large = await ctx.open("approve", Role.APPROVER, hours=1, authority_amount=Decimal("133000.00"))
    small = await ctx.open(
        "approve", Role.APPROVER, hours=2, workflow_id="wf-2", authority_amount=Decimal("36337.50")
    )

    assert [t.id for t in await ctx.service.list_queue(user_id=doctor)] == [small.id]
    assert [t.id for t in await ctx.service.list_queue(user_id=senior)] == [large.id, small.id]
    assert await ctx.service.get_task(task_id=large.id, user_id=doctor) is None
    with pytest.raises(TaskNotAllowed, match="limit"):
        await ctx.service.complete(task_id=large.id, user_id=doctor, outcome={"action": "approve"})
    taken = await ctx.service.take_next(user_id=doctor)
    assert taken is not None and taken.id == small.id
    assert large.authority_amount == Decimal("133000.00")
