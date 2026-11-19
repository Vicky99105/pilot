"""Default TaskService on the claims database, and the Temporal WorkflowSignaller."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.config import Settings
from app.database.models import (
    ClaimRow,
    HospitalRow,
    SubmissionRow,
    TaskRow,
    UserRoleRow,
    UserRow,
    utcnow,
)
from app.services.tasks.contract import (
    Assignee,
    ClaimBrief,
    Task,
    TaskAlreadyDone,
    TaskNotAllowed,
    TaskNotFound,
    WorkflowSignaller,
)
from app.shared.types import DEFAULT_SYSTEM_OF_MEDICINE, Role

AVAILABLE = ("open", "escalated")
UNFINISHED = ("open", "taken", "escalated")
TASK_COMPLETED_SIGNAL = "task_completed"


def _to_task(row: TaskRow, claim: ClaimBrief | None = None) -> Task:
    return Task(
        id=row.id,
        claim_id=row.claim_id,
        workflow_id=row.workflow_id,
        kind=row.kind,
        role=Role(row.role),
        title=row.title,
        payload=row.payload,
        deadline=row.deadline,
        status=row.status,
        assignee=row.assignee,
        outcome=row.outcome,
        authority_amount=row.authority_amount,
        system_of_medicine=row.system_of_medicine,
        created_at=row.created_at,
        claim=claim,
    )


def _amount(raw: object) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw is not None else None
    except ArithmeticError:
        return None


async def _briefs(session: AsyncSession, claim_ids: set[UUID]) -> dict[UUID, ClaimBrief]:
    """Claim number, patient and claimed amount for each claim, in one query."""
    if not claim_ids:
        return {}
    rows = await session.execute(
        select(ClaimRow.id, ClaimRow.claim_number, SubmissionRow.form)
        .outerjoin(SubmissionRow, SubmissionRow.id == ClaimRow.submission_id)
        .where(ClaimRow.id.in_(claim_ids))
    )
    return {
        claim_id: ClaimBrief(
            claim_number=number,
            patient_name=(form or {}).get("patient_name"),
            claimed_amount=_amount((form or {}).get("claimed_amount")),
        )
        for claim_id, number, form in rows
    }


async def _with_briefs(session: AsyncSession, rows: list[TaskRow]) -> list[Task]:
    briefs = await _briefs(session, {r.claim_id for r in rows})
    return [_to_task(r, briefs.get(r.claim_id)) for r in rows]


async def _roles(session: AsyncSession, user_id: UUID) -> dict[str, Decimal | None]:
    """The user's roles and the max_amount on each (empty: no limit)."""
    rows = await session.scalars(select(UserRoleRow).where(UserRoleRow.user_id == user_id))
    return {r.role: r.max_amount for r in rows}


async def _stream(session: AsyncSession, user_id: UUID) -> str:
    """The system of medicine a user reviews: their own, else the default."""
    found = await session.scalar(select(UserRow.system_of_medicine).where(UserRow.id == user_id))
    return found or DEFAULT_SYSTEM_OF_MEDICINE


def _allowed(roles: dict[str, Decimal | None], stream: str) -> ColumnElement[bool]:
    """Tasks of the user's roles whose authority amount, if any, the role's limit covers,
    and whose system of medicine, if any, is the user's. Tasks of a held claim are left out."""
    clauses = [
        TaskRow.role == role
        if limit is None
        else and_(
            TaskRow.role == role,
            or_(TaskRow.authority_amount.is_(None), TaskRow.authority_amount <= limit),
        )
        for role, limit in roles.items()
    ]
    if not clauses:
        return false()
    return and_(
        or_(*clauses),
        or_(TaskRow.system_of_medicine.is_(None), TaskRow.system_of_medicine == stream),
        ~select(ClaimRow.id)
        .where(ClaimRow.id == TaskRow.claim_id, ClaimRow.held_at.is_not(None))
        .exists(),
    )


def _refusal(row: TaskRow, roles: dict[str, Decimal | None], stream: str) -> str | None:
    if row.role not in roles:
        return "user does not hold the task's role"
    limit = roles[row.role]
    if row.authority_amount is not None and limit is not None and row.authority_amount > limit:
        return f"Rs {row.authority_amount:,.2f} is above the user's approval limit"
    if row.system_of_medicine is not None and row.system_of_medicine != stream:
        return f"the task is for the {row.system_of_medicine} system of medicine, not {stream}"
    return None


async def _system_of_medicine(session: AsyncSession, claim_id: UUID) -> str:
    """The treatment's system of medicine: the treating hospital's, else the default."""
    found = await session.scalar(
        select(HospitalRow.system_of_medicine)
        .join(ClaimRow, ClaimRow.hospital_id == HospitalRow.rohini_id)
        .where(ClaimRow.id == claim_id)
    )
    return found or DEFAULT_SYSTEM_OF_MEDICINE


class DefaultTaskService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        signaller: WorkflowSignaller,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._signaller = signaller
        self._now = now

    async def open_task(
        self,
        *,
        claim_id: UUID,
        workflow_id: str,
        kind: str,
        role: Role,
        title: str,
        payload: dict[str, Any],
        deadline: datetime,
        authority_amount: Decimal | None = None,
    ) -> Task:
        """Idempotent for activity retries: an unfinished task of the same kind on the same
        workflow is returned instead of opening a second one."""
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(TaskRow).where(
                    TaskRow.workflow_id == workflow_id,
                    TaskRow.kind == kind,
                    TaskRow.status.in_(UNFINISHED),
                )
            )
            if existing is not None:
                return _to_task(existing)
            row = TaskRow(
                id=uuid4(),
                claim_id=claim_id,
                workflow_id=workflow_id,
                kind=kind,
                role=role.value,
                title=title,
                payload=payload,
                deadline=deadline,
                status="open",
                authority_amount=authority_amount,
                system_of_medicine=await _system_of_medicine(session, claim_id)
                if role is Role.MEDICAL_OFFICER
                else None,
                created_at=self._now(),
            )
            session.add(row)
            return _to_task(row)

    async def list_queue(self, *, user_id: UUID) -> list[Task]:
        async with self._sessions() as session:
            roles = await _roles(session, user_id)
            rows = await session.scalars(
                select(TaskRow)
                .where(
                    _allowed(roles, await _stream(session, user_id)),
                    or_(
                        TaskRow.status.in_(AVAILABLE),
                        and_(TaskRow.status == "taken", TaskRow.assignee == user_id),
                    ),
                )
                .order_by(TaskRow.deadline, TaskRow.created_at)
            )
            return await _with_briefs(session, list(rows))

    async def take_next(self, *, user_id: UUID) -> Task | None:
        """SKIP LOCKED makes concurrent callers pick different rows on Postgres."""
        async with self._sessions.begin() as session:
            roles = await _roles(session, user_id)
            row = await session.scalar(
                select(TaskRow)
                .where(
                    _allowed(roles, await _stream(session, user_id)),
                    TaskRow.status.in_(AVAILABLE),
                )
                .order_by(TaskRow.deadline, TaskRow.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.status, row.assignee, row.taken_at = "taken", user_id, self._now()
            [task] = await _with_briefs(session, [row])
            return task

    async def complete(self, *, task_id: UUID, user_id: UUID, outcome: dict[str, Any]) -> Task:
        async with self._sessions.begin() as session:
            row = await session.get(TaskRow, task_id, with_for_update=True)
            if row is None:
                raise TaskNotFound(str(task_id))
            if row.status == "done":
                raise TaskAlreadyDone(str(task_id))
            refusal = _refusal(row, await _roles(session, user_id), await _stream(session, user_id))
            if refusal is not None:
                raise TaskNotAllowed(refusal)
            if row.status == "taken" and row.assignee != user_id:
                raise TaskNotAllowed("task is taken by another user")
            claim = await session.get(ClaimRow, row.claim_id)
            if claim is not None and claim.held_at is not None:
                raise TaskNotAllowed("the claim is on hold; a supervisor resumes it")
            row.status, row.assignee, row.outcome = "done", user_id, outcome
            row.completed_at = self._now()
            task = _to_task(row)
        # After commit: the workflow only hears about outcomes that are stored.
        await self._signaller.task_completed(
            workflow_id=task.workflow_id,
            task_id=task.id,
            outcome={**outcome, "completed_by": str(user_id)},
        )
        return task

    async def get_task(self, *, task_id: UUID, user_id: UUID) -> Task | None:
        async with self._sessions() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                return None
            roles, stream = await _roles(session, user_id), await _stream(session, user_id)
            if _refusal(row, roles, stream) is not None:
                return None
            [task] = await _with_briefs(session, [row])
            return task

    async def eligible_assignees(self, *, task_id: UUID) -> list[Assignee]:
        async with self._sessions() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFound(str(task_id))
            users = await session.scalars(
                select(UserRow)
                .join(UserRoleRow, UserRoleRow.user_id == UserRow.id)
                .where(UserRoleRow.role == row.role)
                .order_by(UserRow.name)
            )
            found = []
            for user in users:
                roles = await _roles(session, user.id)
                stream = user.system_of_medicine or DEFAULT_SYSTEM_OF_MEDICINE
                if _refusal(row, roles, stream) is None:
                    found.append(Assignee(user_id=user.id, name=user.name))
            return found

    async def reassign(self, *, task_id: UUID, to_user_id: UUID) -> Task:
        async with self._sessions.begin() as session:
            row = await session.get(TaskRow, task_id, with_for_update=True)
            if row is None:
                raise TaskNotFound(str(task_id))
            if row.status == "done":
                raise TaskAlreadyDone(str(task_id))
            refusal = _refusal(
                row, await _roles(session, to_user_id), await _stream(session, to_user_id)
            )
            if refusal is not None:
                raise TaskNotAllowed(f"cannot reassign to that user: {refusal}")
            row.status, row.assignee, row.taken_at = "taken", to_user_id, self._now()
            [task] = await _with_briefs(session, [row])
            return task

    async def shift_deadlines(self, *, claim_id: UUID, seconds: int) -> None:
        if seconds <= 0:
            return
        by = timedelta(seconds=seconds)
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is not None and claim.deadline is not None:
                claim.deadline = claim.deadline + by
            rows = await session.scalars(
                select(TaskRow).where(TaskRow.claim_id == claim_id, TaskRow.status.in_(UNFINISHED))
            )
            for row in rows:
                row.deadline = row.deadline + by


class DefaultWorkflowSignaller:
    """Sends task_completed(task_id, outcome) to the claim's Temporal workflow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None

    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None:
        client = await self._get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(TASK_COMPLETED_SIGNAL, args=[str(task_id), outcome])

    async def _get_client(self) -> Client:
        if self._client is None:
            self._client = await Client.connect(
                self._settings.temporal_address,
                namespace=self._settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            )
        return self._client
