"""Contract for the supervisor: queue depth, tasks near breach, throughput and override rate
per user, held claims; and the supervisor's actions: hold, resume and reassignment.

A supervisor makes no claim decisions. Every action needs the SUPERVISOR role and a reason,
and is recorded as a claim event. A hold stops the claim's workflow before its next stage and
stops its clock; the workflow records the paused clock, and on resume moves the deadlines on
by the time held.

Callers import this module only.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.services.tasks.contract import Task
from app.shared.errors import PermanentError
from app.shared.types import Role

# The task kinds whose outcome can change a machine suggestion, and so count for the
# override rate.
OVERRIDABLE_KINDS = (
    "fix_labels",
    "fix_fields",
    "confirm_registration",
    "confirm_deductions",
    "medical_findings",
    "approve",
)


class QueueDepth(BaseModel):
    role: Role
    unfinished: int  # open, taken and escalated
    taken: int
    overdue: int  # deadline passed
    by_kind: dict[str, int]


class NearBreach(BaseModel):
    task_id: UUID
    claim_id: UUID
    claim_number: str | None
    kind: str
    role: Role
    deadline: datetime
    minutes_left: int  # negative once breached
    assignee: str | None  # the user's name
    held: bool


class UserActivity(BaseModel):
    user_id: UUID
    name: str
    completed: int  # tasks completed in the window (throughput)
    decided: int  # completed tasks of an overridable kind
    overridden: int  # of those, the ones whose outcome changed a machine suggestion
    override_rate: float | None  # overridden / decided; None when nothing was decided


class HeldClaim(BaseModel):
    claim_id: UUID
    claim_number: str | None
    status: str
    held_at: datetime
    held_by: UUID | None
    reason: str


class SupervisorOverview(BaseModel):
    generated_at: datetime
    window_days: int
    near_breach_minutes: int
    queues: list[QueueDepth]  # every review role, empty queues included
    near_breach: list[NearBreach]  # soonest first
    users: list[UserActivity]  # by throughput, highest first
    held_claims: list[HeldClaim]


class NotSupervisor(PermanentError):
    """The user does not hold the SUPERVISOR role."""


class InvalidAction(PermanentError):
    """No reason; or a hold of an unknown, closed or already held claim; or a resume of a
    claim that is not held."""


class ClaimSignaller(Protocol):
    """Sends hold() and resume() to a claim's workflow."""

    async def hold(self, *, workflow_id: str) -> None: ...

    async def resume(self, *, workflow_id: str) -> None: ...


class SupervisionService(Protocol):
    async def overview(
        self, *, by: UUID, window_days: int = 7, near_breach_minutes: int = 60
    ) -> SupervisorOverview: ...

    async def hold(self, claim_id: UUID, reason: str, *, by: UUID) -> HeldClaim:
        """Mark the claim held, record claim_held, then signal the workflow."""
        ...

    async def resume(self, claim_id: UUID, reason: str, *, by: UUID) -> None:
        """Clear the hold, record claim_resumed, then signal the workflow."""
        ...

    async def reassign(self, task_id: UUID, to_user_id: UUID, reason: str, *, by: UUID) -> Task:
        """Give a task to another eligible user and record task_reassigned on its claim."""
        ...
