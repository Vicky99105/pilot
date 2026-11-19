"""Contract for review tasks (human checkpoints) and the workflow signal they send."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Role

TaskStatus = Literal["open", "taken", "done", "escalated"]


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


class TaskNotFound(Exception):
    pass


class TaskNotAllowed(Exception):
    """The user lacks the task's role or the approval limit, or another user took the task."""


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


class WorkflowSignaller(Protocol):
    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None: ...
