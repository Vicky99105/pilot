"""Contract for review tasks (human checkpoints) and the workflow signal they send."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Role, SystemOfMedicine

TaskStatus = Literal["open", "taken", "done", "escalated"]


class ClaimBrief(BaseModel):
    """What a queue row shows about the task's claim."""

    claim_number: str | None
    patient_name: str | None
    claimed_amount: Decimal | None  # as typed on the claim's first submission


class Task(BaseModel):
    id: UUID
    claim_id: UUID
    workflow_id: str
    kind: str
    role: Role
    title: str
    payload: dict[str, Any]
    deadline: datetime
    status: TaskStatus
    assignee: UUID | None
    outcome: dict[str, Any] | None
    # Approver tasks: the user's approver max_amount must cover this (empty: no limit).
    authority_amount: Decimal | None = None
    # Medical officer tasks: the treatment's system of medicine; only medical officers of
    # that system see the task. None for other roles.
    system_of_medicine: SystemOfMedicine | None = None
    created_at: datetime | None = None
    claim: ClaimBrief | None = None  # set on queue reads: list_queue, take_next, get_task


class TaskNotFound(Exception):
    pass


class TaskNotAllowed(Exception):
    """The user lacks the task's role, the approval limit or the system of medicine, another
    user took the task, or the claim is on hold."""


class Assignee(BaseModel):
    user_id: UUID
    name: str


class TaskAlreadyDone(Exception):
    pass


class TaskService(Protocol):
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
    ) -> Task: ...

    async def list_queue(self, *, user_id: UUID) -> list[Task]: ...  # user's roles, by deadline

    async def take_next(self, *, user_id: UUID) -> Task | None: ...  # locks the most urgent task

    async def complete(self, *, task_id: UUID, user_id: UUID, outcome: dict[str, Any]) -> Task:
        """Persists, then signals the workflow with the outcome plus "completed_by"."""
        ...

    async def get_task(self, *, task_id: UUID, user_id: UUID) -> Task | None: ...  # role-checked

    async def eligible_assignees(self, *, task_id: UUID) -> list[Assignee]:
        """Users who may do the task: its role, the approval limit, the system of medicine."""
        ...

    async def reassign(self, *, task_id: UUID, to_user_id: UUID) -> Task:
        """Give an unfinished task to an eligible user, taken by them. The supervisor service
        checks who may reassign and records the audit event."""
        ...

    async def shift_deadlines(self, *, claim_id: UUID, seconds: int) -> None:
        """Move the claim's deadline and its unfinished tasks' deadlines later by seconds: a
        resumed claim gets back the time it was held."""
        ...


class WorkflowSignaller(Protocol):
    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None: ...
