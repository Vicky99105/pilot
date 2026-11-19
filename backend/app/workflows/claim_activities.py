"""ClaimWorkflow activities. Thin wrappers that call service contracts only.

Slice 2: intake, classification and digitisation call their services. The later stages
still only record an event; slices 3 to 5 replace them.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.services.classification.contract import ClassificationService
from app.services.digitisation.contract import DigitisationService
from app.services.intake.contract import IntakeService
from app.services.monitoring.contract import Monitoring
from app.services.tasks.contract import TaskService
from app.shared.errors import PermanentError
from app.shared.types import DocType, Stage
from app.workflows.claim_models import (
    ClaimInput,
    CorrectionInput,
    EventInput,
    OpenTaskInput,
    StageInput,
    StageResult,
)

R = TypeVar("R")


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
    def __init__(
        self,
        monitoring: Monitoring,
        tasks: TaskService,
        *,
        intake: IntakeService,
        classification: ClassificationService,
        digitisation: DigitisationService,
    ) -> None:
        self._monitoring = monitoring
        self._tasks = tasks
        self._intake = intake
        self._classification = classification
        self._digitisation = digitisation

    async def _run(self, claim_id: UUID, stage: Stage, call: Awaitable[R]) -> R:
        """Run a stage's service call. A permanent failure is recorded as an event, so it
        shows in the claim's history, and is not retried."""
        try:
            return await call
        except PermanentError as exc:
            await self._monitoring.record_event(
                claim_id=claim_id,
                stage=stage,
                action="stage_failed",
                actor="system",
                detail={"error": f"{type(exc).__name__}: {exc}"[:2000]},
            )
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}", type="PermanentError", non_retryable=True
            ) from exc

    async def _stage_done(
        self, inp: StageInput, detail: dict[str, Any] | None = None, **result: Any
    ) -> StageResult:
        if inp.submission_id:
            detail = {"submission_id": str(inp.submission_id), **(detail or {})}
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=inp.stage,
                action="stage_completed",
                actor="system",
                detail=detail,
            )
        )
        return StageResult(claim_id=inp.claim_id, stage=inp.stage, **result)

    @activity.defn(name="intake")
    async def intake(self, claim: ClaimInput) -> StageResult:
        submission = await _non_retryable_on_permanent(self._intake.get(claim.submission_id))
        if submission is None:
            raise ApplicationError(
                f"submission {claim.submission_id} not found",
                type="PermanentError",
                non_retryable=True,
            )
        return await self._stage_done(
            StageInput(
                claim_id=submission.claim_id,
                stage=Stage.INTAKE,
                submission_id=submission.submission_id,
            ),
            {
                "files": len(submission.files),
                "missing_documents": [d.value for d in submission.missing_documents],
            },
        )

    @activity.defn(name="classification")
    async def classification(self, inp: StageInput) -> StageResult:
        if inp.submission_id is None:
            raise ApplicationError(
                "classification needs a submission", type="PermanentError", non_retryable=True
            )
        doc_set = await self._run(
            inp.claim_id, inp.stage, self._classification.classify(inp.submission_id)
        )
        payload = {
            "submission_id": str(doc_set.submission_id),
            "pages": [
                {
                    "page_id": str(p.page_id),
                    "seq": p.seq,
                    "doc_type": p.doc_type.value,
                    "confidence": p.confidence,
                    "image_key": p.image_key,
                    "quality_warning": p.quality_warning,
                }
                for p in doc_set.pages
            ],
            "mismatches": [m.model_dump(mode="json") for m in doc_set.mismatches],
        }
        return await self._stage_done(
            inp,
            {
                "pages": len(doc_set.pages),
                "documents": [d.doc_type.value for d in doc_set.documents],
                "needs_review": doc_set.needs_review,
            },
            needs_checkpoint=doc_set.needs_review,
            checkpoint_payload=payload,
        )

    @activity.defn(name="digitisation")
    async def digitisation(self, inp: StageInput) -> StageResult:
        record = await self._run(inp.claim_id, inp.stage, self._digitisation.extract(inp.claim_id))
        payload = {
            "low_confidence_fields": record.low_confidence_fields,
            "failed_checks": [c.model_dump(mode="json") for c in record.failed_checks],
        }
        return await self._stage_done(
            inp,
            payload,
            needs_checkpoint=bool(record.low_confidence_fields or record.failed_checks),
            checkpoint_payload=payload,
        )

    @activity.defn(name="apply_corrections")
    async def apply_corrections(self, inp: CorrectionInput) -> None:
        if inp.stage is Stage.CLASSIFICATION:
            labels = _labels(inp.outcome.get("labels") or {})
            call: Awaitable[Any] = self._classification.apply_labels(
                inp.claim_id, labels, actor=inp.actor
            )
            changed = sorted(str(p) for p in labels)
        elif inp.stage is Stage.DIGITISATION:
            fields = inp.outcome.get("fields") or {}
            call = self._digitisation.apply_corrections(inp.claim_id, fields, actor=inp.actor)
            changed = sorted(fields)
        else:
            raise ApplicationError(
                f"{inp.stage} takes no corrections", type="PermanentError", non_retryable=True
            )
        await self._run(inp.claim_id, inp.stage, call)
        await self._monitoring.record_event(
            claim_id=inp.claim_id,
            stage=inp.stage,
            action="corrections_applied",
            actor=inp.actor,
            detail={"changed": changed},
        )

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
        names = [s.value for s in Stage] + ["open_task", "record_event", "apply_corrections"]
        return [getattr(self, n) for n in names]


def _labels(raw: dict[str, Any]) -> dict[UUID, DocType]:
    try:
        return {UUID(page_id): DocType(doc_type) for page_id, doc_type in raw.items()}
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            f"labels must map page IDs to document types: {exc}",
            type="PermanentError",
            non_retryable=True,
        ) from exc
