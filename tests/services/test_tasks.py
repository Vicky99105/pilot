from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, HospitalRow, SubmissionRow
from app.services.tasks.contract import (
    ClaimBrief,
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


async def test_queue_tasks_carry_the_claim_brief_and_when_they_opened(ctx: Ctx) -> None:
    """The queue shows claim number, patient and claimed amount without a call per row."""
    async with ctx.sessions.begin() as session:
        claim = await session.get(ClaimRow, ctx.claim_id)
        assert claim is not None
        submission = SubmissionRow(
            id=uuid4(),
            request_type="reimbursement",
            claim_id=ctx.claim_id,
            form={"patient_name": "Asha Verma", "claimed_amount": "133000.00"},
        )
        session.add(submission)
        await session.flush()
        claim.claim_number, claim.submission_id = "CLM-7", submission.id
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    opened = await ctx.open("fix_labels", Role.CLAIMS_OPERATOR, hours=4)

    [queued] = await ctx.service.list_queue(user_id=ops)
    taken = await ctx.service.take_next(user_id=ops)
    fetched = await ctx.service.get_task(task_id=opened.id, user_id=ops)

    expected = ClaimBrief(
        claim_number="CLM-7", patient_name="Asha Verma", claimed_amount=Decimal("133000.00")
    )
    for task in (queued, taken, fetched):
        assert task is not None
        assert task.claim == expected
        assert task.created_at is not None


# --- P2-6: system of medicine, held claims, reassignment -----------------------------------


async def at_hospital(ctx: Ctx, system_of_medicine: str) -> None:
    """Put ctx's claim at a hospital of that system of medicine."""
    async with ctx.sessions.begin() as session:
        session.add(
            HospitalRow(rohini_id="8900000000097", name="Ayush Wellness Centre", city="Pune",
                        state="Maharashtra", network=True, differential_billing=False,
                        system_of_medicine=system_of_medicine, source="test",
                        source_version="v")
        )  # fmt: skip
        claim = await session.get(ClaimRow, ctx.claim_id)
        assert claim is not None
        claim.hospital_id = "8900000000097"


async def test_a_medical_task_carries_the_treatments_system_of_medicine(ctx: Ctx) -> None:
    await at_hospital(ctx, "ayush")
    allopathy = await add_user(ctx.sessions, "allopath", [Role.MEDICAL_OFFICER],
                               system_of_medicine="allopathy")  # fmt: skip
    ayush = await add_user(ctx.sessions, "vaidya", [Role.MEDICAL_OFFICER],
                           system_of_medicine="ayush")  # fmt: skip

    task = await ctx.open("medical_findings", Role.MEDICAL_OFFICER, hours=1)

    assert task.system_of_medicine == "ayush"
    assert await ctx.service.list_queue(user_id=allopathy) == []
    assert await ctx.service.take_next(user_id=allopathy) is None
    assert await ctx.service.get_task(task_id=task.id, user_id=allopathy) is None
    with pytest.raises(TaskNotAllowed, match="system of medicine"):
        await ctx.service.complete(task_id=task.id, user_id=allopathy, outcome={})
    assert [t.id for t in await ctx.service.list_queue(user_id=ayush)] == [task.id]


async def test_a_medical_task_at_an_allopathic_or_unknown_hospital_is_allopathy(
    ctx: Ctx,
) -> None:
    doctor = await add_user(ctx.sessions, "doctor", [Role.MEDICAL_OFFICER])  # no stream set

    task = await ctx.open("medical_findings", Role.MEDICAL_OFFICER, hours=1)
    other = await ctx.open("confirm_deductions", Role.BILL_PROCESSOR, hours=1, workflow_id="wf-9")

    assert (task.system_of_medicine, other.system_of_medicine) == ("allopathy", None)
    assert [t.id for t in await ctx.service.list_queue(user_id=doctor)] == [task.id]


async def test_tasks_of_a_held_claim_leave_the_queues_and_cannot_be_completed(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    task = await ctx.open("confirm_registration", Role.CLAIMS_OPERATOR, hours=1)
    async with ctx.sessions.begin() as session:
        claim = await session.get(ClaimRow, ctx.claim_id)
        assert claim is not None
        claim.held_at, claim.hold_reason = NOW, "Fraud tip-off"

    assert await ctx.service.list_queue(user_id=ops) == []
    assert await ctx.service.take_next(user_id=ops) is None
    with pytest.raises(TaskNotAllowed, match="on hold"):
        await ctx.service.complete(task_id=task.id, user_id=ops, outcome={})


async def test_a_task_is_reassigned_only_to_an_eligible_user(ctx: Ctx) -> None:
    first = await add_user(ctx.sessions, "first", [Role.APPROVER], max_amount=Decimal("100000"))
    second = await add_user(ctx.sessions, "second", [Role.APPROVER], max_amount=Decimal("100000"))
    low = await add_user(ctx.sessions, "low", [Role.APPROVER], max_amount=Decimal("10000"))
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    task = await ctx.open("approve", Role.APPROVER, hours=1, authority_amount=Decimal("50000"))
    assert await ctx.service.take_next(user_id=first) is not None

    eligible = await ctx.service.eligible_assignees(task_id=task.id)
    moved = await ctx.service.reassign(task_id=task.id, to_user_id=second)

    assert {a.user_id for a in eligible} == {first, second}
    assert (moved.status, moved.assignee) == ("taken", second)
    assert [t.id for t in await ctx.service.list_queue(user_id=second)] == [task.id]
    assert await ctx.service.list_queue(user_id=first) == []
    for refused in (low, ops):
        with pytest.raises(TaskNotAllowed):
            await ctx.service.reassign(task_id=task.id, to_user_id=refused)
    await ctx.service.complete(task_id=task.id, user_id=second, outcome={"action": "approve"})
    with pytest.raises(TaskAlreadyDone):
        await ctx.service.reassign(task_id=task.id, to_user_id=first)


async def test_shift_deadlines_moves_the_claim_and_its_open_tasks(ctx: Ctx) -> None:
    ops = await add_user(ctx.sessions, "ops", [Role.CLAIMS_OPERATOR])
    done = await ctx.open("a", Role.CLAIMS_OPERATOR, hours=1, workflow_id="wf-a")
    await ctx.service.complete(task_id=done.id, user_id=ops, outcome={})
    open_task = await ctx.open("b", Role.CLAIMS_OPERATOR, hours=2, workflow_id="wf-b")
    async with ctx.sessions.begin() as session:
        claim = await session.get(ClaimRow, ctx.claim_id)
        assert claim is not None
        claim.deadline = NOW

    await ctx.service.shift_deadlines(claim_id=ctx.claim_id, seconds=3600)

    queue = await ctx.service.list_queue(user_id=ops)
    assert [(t.id, t.deadline) for t in queue] == [(open_task.id, NOW + timedelta(hours=3))]
    async with ctx.sessions() as session:
        claim = await session.get(ClaimRow, ctx.claim_id)
    assert claim is not None and claim.deadline == NOW + timedelta(hours=1)
