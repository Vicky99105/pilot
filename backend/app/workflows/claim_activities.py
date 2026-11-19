"""ClaimWorkflow activities. Thin wrappers that call service contracts only.

Slice 1: every stage only records an event. Slices 2 to 5 replace each body with the real
stage service call.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.services.monitoring.contract import Monitoring
from app.services.tasks.contract import TaskService
from app.shared.errors import PermanentError
from app.shared.types import Stage
from app.workflows.claim_models import (
    ClaimInput,
    EventInput,
    OpenTaskInput,
    StageInput,
    StageResult,
)

R = TypeVar("R")
_CLAIM_NAMESPACE = uuid.UUID("6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0001")


async def _non_retryable_on_permanent(call: Awaitable[R]) -> R:
    """Temporal matches non-retryable types by exact class name, so subclasses of
    PermanentError are re-raised as one non-retryable PermanentError."""
    try:
        return await call
    except PermanentError as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}", type="PermanentError", non_retryable=True
        ) from exc


class ClaimActivities:
    def __init__(self, monitoring: Monitoring, tasks: TaskService) -> None:
        self._monitoring = monitoring
        self._tasks = tasks

    async def _stage_done(self, inp: StageInput, **detail: Any) -> StageResult:
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=inp.stage,
                action="stage_completed",
                actor="system",
                detail={"submission_id": str(inp.submission_id)} if inp.submission_id else None,
            )
        )
        return StageResult(claim_id=inp.claim_id, stage=inp.stage, **detail)

    @activity.defn(name="intake")
    async def intake(self, claim: ClaimInput) -> StageResult:
        # Placeholder ID until intake creates the claims row (slice 2).
        claim_id = uuid.uuid5(_CLAIM_NAMESPACE, str(claim.submission_id))
        return await self._stage_done(StageInput(claim_id=claim_id, stage=Stage.INTAKE))

    @activity.defn(name="classification")
    async def classification(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="digitisation")
    async def digitisation(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="registration")
    async def registration(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="bill_review")
    async def bill_review(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="medical_review")
    async def medical_review(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="risk")
    async def risk(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="adjudication")
    async def adjudication(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp, draft="approve")

    @activity.defn(name="decision")
    async def decision(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="communication")
    async def communication(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="query")
    async def query(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="settlement")
    async def settlement(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="monitoring")
    async def monitoring(self, inp: StageInput) -> StageResult:
        return await self._stage_done(inp)

    @activity.defn(name="open_task")
    async def open_task(self, inp: OpenTaskInput) -> UUID:
        task = await _non_retryable_on_permanent(
            self._tasks.open_task(
                claim_id=inp.claim_id,
                workflow_id=inp.workflow_id,
                kind=inp.kind,
                role=inp.role,
                title=inp.title,
                payload=inp.payload,
                deadline=inp.deadline,
            )
        )
        await self._monitoring.record_event(
            claim_id=inp.claim_id,
            stage=inp.stage,
            action="checkpoint_opened",
            actor="workflow",
            detail={"task_id": str(task.id), "kind": inp.kind, "role": inp.role.value},
        )
        return task.id

    @activity.defn(name="record_event")
    async def record_event(self, inp: EventInput) -> None:
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=inp.stage,
                action=inp.action,
                actor=inp.actor,
                detail=inp.detail,
            )
        )

    def all(self) -> list[Callable[..., Awaitable[Any]]]:
        names = [s.value for s in Stage] + ["open_task", "record_event"]
        return [getattr(self, n) for n in names]
