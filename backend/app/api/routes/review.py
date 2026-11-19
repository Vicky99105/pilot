"""Review area for P2-6: the supervisor view, hold and resume, reassignment and the override
log. The supervisor service checks the SUPERVISOR role; routes map its refusals to HTTP."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import Overrides, StaffUser, Supervision, Tasks
from app.services.overrides.contract import Override
from app.services.supervision.contract import (
    HeldClaim,
    InvalidAction,
    NotSupervisor,
    SupervisorOverview,
)
from app.services.tasks.contract import (
    Assignee,
    Task,
    TaskAlreadyDone,
    TaskNotAllowed,
    TaskNotFound,
)
from app.shared.types import Role

router = APIRouter(prefix="/api", tags=["review"])


class Reason(BaseModel):
    reason: str


class Reassignment(BaseModel):
    to_user_id: UUID
    reason: str


def _supervisor(user: StaffUser) -> None:
    if Role.SUPERVISOR not in user.roles:
        raise HTTPException(403, "supervisors only")


@router.get("/supervisor/overview", response_model=SupervisorOverview)
async def overview(
    user: StaffUser,
    supervision: Supervision,
    window_days: int = 7,
    near_breach_minutes: int = 60,
) -> SupervisorOverview:
    try:
        return await supervision.overview(
            by=user.id, window_days=window_days, near_breach_minutes=near_breach_minutes
        )
    except NotSupervisor as exc:
        raise HTTPException(403, str(exc)) from None


@router.post("/claims/{claim_id}/hold", response_model=HeldClaim)
async def hold(
    claim_id: UUID, body: Reason, user: StaffUser, supervision: Supervision
) -> HeldClaim:
    try:
        return await supervision.hold(claim_id, body.reason, by=user.id)
    except NotSupervisor as exc:
        raise HTTPException(403, str(exc)) from None
    except InvalidAction as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/claims/{claim_id}/resume", status_code=204)
async def resume(
    claim_id: UUID, body: Reason, user: StaffUser, supervision: Supervision
) -> Response:
    try:
        await supervision.resume(claim_id, body.reason, by=user.id)
    except NotSupervisor as exc:
        raise HTTPException(403, str(exc)) from None
    except InvalidAction as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(status_code=204)


@router.get("/tasks/{task_id}/assignees", response_model=list[Assignee])
async def assignees(task_id: UUID, user: StaffUser, tasks: Tasks) -> list[Assignee]:
    _supervisor(user)
    try:
        return await tasks.eligible_assignees(task_id=task_id)
    except TaskNotFound:
        raise HTTPException(404, "task not found") from None


@router.post("/tasks/{task_id}/reassign", response_model=Task)
async def reassign(
    task_id: UUID, body: Reassignment, user: StaffUser, supervision: Supervision
) -> Task:
    try:
        return await supervision.reassign(task_id, body.to_user_id, body.reason, by=user.id)
    except NotSupervisor as exc:
        raise HTTPException(403, str(exc)) from None
    except TaskNotFound:
        raise HTTPException(404, "task not found") from None
    except TaskNotAllowed as exc:
        raise HTTPException(403, str(exc)) from None
    except (TaskAlreadyDone, InvalidAction) as exc:
        raise HTTPException(409, str(exc) or "task already completed") from None


@router.get("/overrides", response_model=list[Override])
async def overrides(
    user: StaffUser,
    log: Overrides,
    claim_id: UUID | None = None,
    user_id: UUID | None = None,
    limit: int = 200,
) -> list[Override]:
    """Every change a person made to a machine suggestion, newest first. Staff only."""
    return await log.list(claim_id=claim_id, user_id=user_id, limit=min(limit, 1000))
