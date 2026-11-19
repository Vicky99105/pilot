from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.services.tasks.contract import Task, TaskAlreadyDone, TaskNotAllowed, TaskNotFound
from app.shared.types import Role


class FakeWorkflowSignaller:
    def __init__(self) -> None:
        self.signals: list[tuple[str, UUID, dict[str, Any]]] = []

    async def task_completed(
        self, *, workflow_id: str, task_id: UUID, outcome: dict[str, Any]
    ) -> None:
        self.signals.append((workflow_id, task_id, outcome))


class FakeTaskService:
    """Implements the TaskService contract in memory. roles maps user ID to roles."""

    def __init__(
        self,
        roles: dict[UUID, set[Role]] | None = None,
        signaller: FakeWorkflowSignaller | None = None,
    ) -> None:
        self.roles = roles or {}
        self.signaller = signaller or FakeWorkflowSignaller()
        self.tasks: dict[UUID, Task] = {}
        self.shifted: list[tuple[UUID, int]] = []  # shift_deadlines calls: (claim, seconds)

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
        for t in self.tasks.values():
            if (t.workflow_id, t.kind) == (workflow_id, kind) and t.status != "done":
                return t
        task = Task(
            id=uuid4(),
            claim_id=claim_id,
            workflow_id=workflow_id,
            kind=kind,
            role=role,
            title=title,
            payload=payload,
            deadline=deadline,
            status="open",
            assignee=None,
            outcome=None,
            authority_amount=authority_amount,
        )
        self.tasks[task.id] = task
        return task

    def unfinished(self, kind: str | None = None) -> list[Task]:
        return [
            t
            for t in self.tasks.values()
            if t.status != "done" and (kind is None or t.kind == kind)
        ]

    async def list_queue(self, *, user_id: UUID) -> list[Task]:
        mine = self.roles.get(user_id, set())
        return sorted(
            (
                t
                for t in self.tasks.values()
                if t.role in mine
                and (t.status in ("open", "escalated") or t.assignee == user_id)
                and t.status != "done"
            ),
            key=lambda t: t.deadline,
        )

    async def take_next(self, *, user_id: UUID) -> Task | None:
        for t in await self.list_queue(user_id=user_id):
            if t.status != "taken":
                taken = t.model_copy(update={"status": "taken", "assignee": user_id})
                self.tasks[t.id] = taken
                return taken
        return None

    async def complete(self, *, task_id: UUID, user_id: UUID, outcome: dict[str, Any]) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound(str(task_id))
        if task.status == "done":
            raise TaskAlreadyDone(str(task_id))
        if task.role not in self.roles.get(user_id, set()):
            raise TaskNotAllowed("role")
        done = task.model_copy(update={"status": "done", "assignee": user_id, "outcome": outcome})
        self.tasks[task_id] = done
        await self.signaller.task_completed(
            workflow_id=task.workflow_id,
            task_id=task_id,
            outcome={**outcome, "completed_by": str(user_id)},
        )
        return done

    async def get_task(self, *, task_id: UUID, user_id: UUID) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None or task.role not in self.roles.get(user_id, set()):
            return None
        return task

    async def shift_deadlines(self, *, claim_id: UUID, seconds: int) -> None:
        self.shifted.append((claim_id, seconds))
