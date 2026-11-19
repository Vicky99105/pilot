from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import CurrentUser, Tasks
from app.services.tasks.contract import Task, TaskAlreadyDone, TaskNotAllowed, TaskNotFound

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class CompleteRequest(BaseModel):
    outcome: dict[str, Any]


@router.get("/queue", response_model=list[Task])
async def queue(user: CurrentUser, tasks: Tasks) -> list[Task]:
    return await tasks.list_queue(user_id=user.id)


@router.post("/next", response_model=Task, responses={204: {"description": "queue empty"}})
async def take_next(user: CurrentUser, tasks: Tasks) -> Task | Response:
    task = await tasks.take_next(user_id=user.id)
    return task if task is not None else Response(status_code=204)


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: UUID, user: CurrentUser, tasks: Tasks) -> Task:
    task = await tasks.get_task(task_id=task_id, user_id=user.id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@router.post("/{task_id}/complete", response_model=Task)
async def complete(task_id: UUID, body: CompleteRequest, user: CurrentUser, tasks: Tasks) -> Task:
    try:
        return await tasks.complete(task_id=task_id, user_id=user.id, outcome=body.outcome)
    except TaskNotFound:
        raise HTTPException(404, "task not found") from None
    except TaskNotAllowed as exc:
        raise HTTPException(403, str(exc)) from None
    except TaskAlreadyDone:
        raise HTTPException(409, "task already completed") from None
