"""ClaimWorkflow: one workflow per claim, through all 13 stages with human checkpoints.

Checkpoint pattern: the open_task activity, then wait for that task's outcome through the
task_completed signal. When the deadline passes, an escalation event is recorded and the
workflow keeps waiting. A send-back outcome re-runs the named stage. An outcome the
service refuses (a bad correction, a decision above the approver's limit, a release with
failed payee checks) sends the same checkpoint back to the queue with the reason.

Order at the end: decision (approver, then CRC for a partial or reject), communication,
then settlement: payee checks, the finance release, then payment, block release, closure.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.shared.types import Role, Stage
    from app.workflows.claim_activities import MODEL_UNAVAILABLE, ClaimActivities
    from app.workflows.claim_models import (
        ClaimInput,
        ClaimResult,
        CorrectionInput,
        DecisionStep,
        EventInput,
        FinalDecision,
        OpenTaskInput,
        OutcomeInput,
        QueryRaised,
        QueryStepInput,
        StageInput,
        StageResult,
    )

CHECKPOINT_SLA = timedelta(hours=4)
ACTIVITY_TIMEOUT = timedelta(minutes=5)
RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["PermanentError"])
# Activities that call a model. The cap is per activity: the activity is the unit of work.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=8,
    non_retryable_error_types=["PermanentError"],
)
PAID: tuple[FinalDecision, ...] = ("approve", "partial")

# Stages after intake, in order. Query and monitoring run outside this list. Medical review
# runs in two steps around bill review: coding first, because bill review maps the verified
# procedures to a package; the full review after, because it reads that package's guideline.
PIPELINE = [
    Stage.CLASSIFICATION,
    Stage.DIGITISATION,
    Stage.REGISTRATION,
    Stage.MEDICAL_CODING,
    Stage.BILL_REVIEW,
    Stage.MEDICAL_REVIEW,
    Stage.RISK,
    Stage.ADJUDICATION,
    Stage.DECISION,
    Stage.COMMUNICATION,
    Stage.SETTLEMENT,
]
# Stages whose activity calls a model, directly or through a service.
MODEL_STAGES = {
    Stage.CLASSIFICATION,
    Stage.DIGITISATION,
    Stage.MEDICAL_CODING,
    Stage.BILL_REVIEW,
    Stage.MEDICAL_REVIEW,
    Stage.RISK,
    Stage.ADJUDICATION,
    Stage.DECISION,
    Stage.COMMUNICATION,
}
# Where a query reply resumes: medical coding onward. The reply's documents can change the
# codes, so the package and the deductions, and the query items are re-evaluated.
RESUME_AFTER_REPLY = Stage.MEDICAL_CODING

STAGE_ACTIVITIES = {
    Stage.CLASSIFICATION: ClaimActivities.classification,
    Stage.DIGITISATION: ClaimActivities.digitisation,
    Stage.REGISTRATION: ClaimActivities.registration,
    Stage.MEDICAL_CODING: ClaimActivities.medical_coding,
    Stage.BILL_REVIEW: ClaimActivities.bill_review,
    Stage.MEDICAL_REVIEW: ClaimActivities.medical_review,
    Stage.RISK: ClaimActivities.risk,
    Stage.ADJUDICATION: ClaimActivities.adjudication,
    Stage.DECISION: ClaimActivities.decision,
    Stage.COMMUNICATION: ClaimActivities.communication,
    Stage.SETTLEMENT: ClaimActivities.settlement,
    Stage.MONITORING: ClaimActivities.monitoring,
}


@dataclass(frozen=True)
class Checkpoint:
    kind: str
    role: Role
    title: str


FIX_LABELS = Checkpoint("fix_labels", Role.CLAIMS_OPERATOR, "Fix document labels")
FIX_FIELDS = Checkpoint("fix_fields", Role.CLAIMS_OPERATOR, "Fix extracted fields")
CONFIRM_REGISTRATION = Checkpoint(
    "confirm_registration", Role.CLAIMS_OPERATOR, "Confirm registration"
)
CONFIRM_DEDUCTIONS = Checkpoint("confirm_deductions", Role.BILL_PROCESSOR, "Confirm deductions")
MEDICAL_FINDINGS = Checkpoint("medical_findings", Role.MEDICAL_OFFICER, "Confirm findings")
INVESTIGATE = Checkpoint("investigate", Role.INVESTIGATOR, "Investigate referred claim")
APPROVE = Checkpoint("approve", Role.APPROVER, "Approve, override or send back")
CRC = Checkpoint("crc", Role.CRC_REVIEWER, "Approve rejection or partial")
QUERY_NO_REPLY = Checkpoint("query_no_reply", Role.CLAIMS_OPERATOR, "Handle query with no reply")
RELEASE_PAYMENT = Checkpoint("release_payment", Role.FINANCE, "Release payment")

# Stages whose checkpoint outcome can carry corrections, and the outcome key that holds them.
CORRECTABLE = {
    Stage.CLASSIFICATION: "labels",
    Stage.DIGITISATION: "fields",
    Stage.REGISTRATION: "overrides",
    Stage.BILL_REVIEW: "lines",
}

# Stages whose checkpoint outcome an activity records. A refused outcome reopens the task.
RECORDED = {
    Stage.MEDICAL_REVIEW: ClaimActivities.medical_outcome,
    Stage.RISK: ClaimActivities.investigation_outcome,
}

CONDITIONAL = {
    Stage.CLASSIFICATION: FIX_LABELS,
    Stage.DIGITISATION: FIX_FIELDS,
    Stage.MEDICAL_REVIEW: MEDICAL_FINDINGS,
    Stage.RISK: INVESTIGATE,
}


def checkpoints_after(stage: Stage, result: StageResult) -> list[Checkpoint]:
    """Checkpoints of the stages before the decision. Decision and settlement have their
    own steps in the workflow."""
    if stage in CONDITIONAL:
        return [CONDITIONAL[stage]] if result.needs_checkpoint else []
    if stage is Stage.REGISTRATION:
        return [CONFIRM_REGISTRATION]
    if stage is Stage.BILL_REVIEW:
        return [CONFIRM_DEDUCTIONS]
    return []


def send_back_target(outcome: dict[str, Any], reachable: list[Stage]) -> Stage | None:
    """The stage a send-back outcome names, if it is this stage or an earlier one."""
    if outcome.get("action") != "send_back":
        return None
    target = outcome.get("stage")
    return next((s for s in reachable if s.value == target), None)


def _reason(err: ActivityError) -> str:
    return str(err.cause) if err.cause else str(err)


@workflow.defn
class ClaimWorkflow:
    def __init__(self) -> None:
        self._outcomes: dict[str, dict[str, Any]] = {}
        self._followups: list[str] = []
        self._followups_used = 0
        self._held = False
        self._claim_id: UUID | None = None
        self._submission_id: UUID | None = None
        self._final: FinalDecision | None = None
        self._last_task_id = ""

    @workflow.signal
    def task_completed(self, task_id: str, outcome: dict[str, Any]) -> None:
        self._outcomes[task_id] = outcome

    @workflow.signal
    def followup_received(self, submission_id: str) -> None:
        self._followups.append(submission_id)

    @workflow.signal
    def hold(self) -> None:
        self._held = True

    @workflow.signal
    def resume(self) -> None:
        self._held = False

    @workflow.run
    async def run(self, claim: ClaimInput) -> ClaimResult:
        self._submission_id = claim.submission_id
        intake = await workflow.execute_activity_method(
            ClaimActivities.intake,
            claim,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        claim_id = self._claim_id = intake.claim_id
        i = 0
        while i < len(PIPELINE):
            stage = PIPELINE[i]
            if stage is Stage.DECISION:
                target = await self._decide()
            elif stage is Stage.SETTLEMENT:
                target = await self._settle(PIPELINE[: i + 1])
            else:
                result = await self._run_stage(stage)
                if stage is Stage.ADJUDICATION and result.draft == "query":
                    resume = await self._query_loop()
                    if resume is not None:
                        i = PIPELINE.index(resume)
                        continue
                target = await self._checkpoints(stage, result, PIPELINE[: i + 1])
            if target is not None:
                await self._event(stage, "sent_back", {"to": target.value})
                i = PIPELINE.index(target)
                continue
            i += 1
        await self._run_stage(Stage.MONITORING)
        return ClaimResult(claim_id=claim_id, status="closed", draft=self._final)

    async def _checkpoints(
        self, stage: Stage, result: StageResult, reachable: list[Stage]
    ) -> Stage | None:
        """Run a stage's checkpoints. Returns a send-back target, if any."""
        for checkpoint in checkpoints_after(stage, result):
            outcome = await self._checkpoint(stage, checkpoint, result.checkpoint_payload)
            if stage in CORRECTABLE:
                outcome = await self._corrected(
                    stage, checkpoint, outcome, result.checkpoint_payload
                )
            if stage in RECORDED:
                outcome = await self._recorded(
                    stage, checkpoint, outcome, result.checkpoint_payload, reachable
                )
            target = send_back_target(outcome, reachable)
            if target is not None:
                return target
        return None

    async def _recorded(
        self,
        stage: Stage,
        checkpoint: Checkpoint,
        outcome: dict[str, Any],
        payload: dict[str, Any],
        reachable: list[Stage],
    ) -> dict[str, Any]:
        """Record a person's outcome through the stage's service. An outcome the service
        refuses sends the same checkpoint back to the queue with the reason. A send-back is
        returned as it is."""
        assert self._claim_id is not None
        while send_back_target(outcome, reachable) is None:
            try:
                await workflow.execute_activity_method(
                    RECORDED[stage],
                    OutcomeInput(
                        claim_id=self._claim_id,
                        stage=stage,
                        task_id=UUID(self._last_task_id),
                        outcome=outcome,
                    ),
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=RETRY,
                )
                return outcome
            except ActivityError as err:
                reason = _reason(err)
                await self._event(
                    stage, "outcome_refused", {"kind": checkpoint.kind, "reason": reason}
                )
                outcome = await self._checkpoint(stage, checkpoint, {**payload, "error": reason})
        return outcome

    async def _decide(self) -> Stage | None:
        """Approver, then the CRC for a partial or reject. Returns a send-back target."""
        routed = await self._run_stage(Stage.DECISION)
        payload = routed.checkpoint_payload
        step: DecisionStep = await self._until_accepted(
            Stage.DECISION,
            APPROVE,
            payload,
            ClaimActivities.record_decision,
            authority_amount=routed.authority_amount,
        )
        if step.send_back_stage is not None:
            return step.send_back_stage
        if step.crc_required:
            crc_payload = {**payload, "final_decision": step.final_decision}
            step = await self._until_accepted(
                Stage.DECISION, CRC, crc_payload, ClaimActivities.review_crc
            )
            if step.send_back_stage is not None:
                return step.send_back_stage
        self._final = step.final_decision
        return None

    async def _settle(self, reachable: list[Stage]) -> Stage | None:
        """Payee checks, then the finance release for a paid claim, then settle."""
        checked = await self._run_stage(Stage.SETTLEMENT)
        if self._final in PAID:
            outcome = await self._checkpoint(
                Stage.SETTLEMENT, RELEASE_PAYMENT, checked.checkpoint_payload
            )
            target = send_back_target(outcome, reachable)
            if target is not None:
                return target
            await self._until_accepted(
                Stage.SETTLEMENT,
                RELEASE_PAYMENT,
                checked.checkpoint_payload,
                ClaimActivities.settle,
                outcome=outcome,
            )
        else:
            assert self._claim_id is not None
            await workflow.execute_activity_method(
                ClaimActivities.settle,
                OutcomeInput(
                    claim_id=self._claim_id, stage=Stage.SETTLEMENT, task_id=UUID(int=0), outcome={}
                ),
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            )
        return None

    async def _until_accepted(
        self,
        stage: Stage,
        checkpoint: Checkpoint,
        payload: dict[str, Any],
        activity: Callable[..., Any],
        *,
        authority_amount: Decimal | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> Any:
        """Open the checkpoint (unless its outcome is given) and record the outcome with
        the activity. An outcome the service refuses reopens the checkpoint with the reason."""
        assert self._claim_id is not None
        error: str | None = None
        while True:
            if outcome is None:
                extra = {"error": error} if error else {}
                outcome = await self._checkpoint(
                    stage, checkpoint, {**payload, **extra}, authority_amount=authority_amount
                )
            try:
                return await workflow.execute_activity_method(
                    activity,
                    OutcomeInput(
                        claim_id=self._claim_id,
                        stage=stage,
                        task_id=UUID(self._last_task_id),
                        outcome=outcome,
                    ),
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=RETRY,
                )
            except ActivityError as err:
                error = _reason(err)
                await self._event(
                    stage, "outcome_refused", {"kind": checkpoint.kind, "reason": error}
                )
                outcome = None

    async def _query_loop(self) -> Stage | None:
        """Raise the query and wait for a reply, with reminders. A reply re-runs
        classification and digitisation on it, then resumes at medical coding. With no
        reply by the deadline, a claims operator decides: wait again ("extend", the
        default) or go to the approver as the claim stands ("proceed")."""
        assert self._claim_id is not None
        raised: QueryRaised = await workflow.execute_activity_method(
            ClaimActivities.query,
            StageInput(claim_id=self._claim_id, stage=Stage.QUERY),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        if raised.query_id is None:
            return None  # nothing new to ask: the approver decides on the draft
        step = QueryStepInput(claim_id=self._claim_id, query_id=raised.query_id)
        reminders = raised.reminder_days
        while True:
            start = workflow.now()
            reply = None
            for day in reminders:
                reply = await self._next_followup(until=start + timedelta(days=day))
                if reply is not None:
                    break
                await self._query_step(ClaimActivities.query_reminder, step, day=day)
            if reply is None:
                reply = await self._next_followup(
                    until=start + timedelta(days=raised.reply_window_days)
                )
            if reply is not None:
                await self._query_step(ClaimActivities.query_reply, step, submission_id=reply)
                for stage in (Stage.CLASSIFICATION, Stage.DIGITISATION):
                    result = await self._run_stage(stage, submission_id=reply)
                    await self._checkpoints(stage, result, [])
                return RESUME_AFTER_REPLY
            await self._query_step(ClaimActivities.query_overdue, step)
            outcome = await self._checkpoint(
                Stage.QUERY, QUERY_NO_REPLY, {"query_id": str(raised.query_id)}
            )
            if outcome.get("action") == "proceed":
                await self._event(Stage.QUERY, "query_closed_without_reply")
                return None
            await self._event(Stage.QUERY, "query_extended")
            reminders = []  # the claimant was reminded already

    async def _next_followup(self, *, until: datetime) -> UUID | None:
        """The next unused follow-up, waiting for one until the given time."""
        timeout = until - workflow.now()
        if timeout > timedelta(0):
            try:
                await workflow.wait_condition(
                    lambda: len(self._followups) > self._followups_used, timeout=timeout
                )
            except TimeoutError:
                return None
        if len(self._followups) > self._followups_used:
            self._followups_used += 1
            return UUID(self._followups[self._followups_used - 1])
        return None

    async def _query_step(
        self,
        activity: Callable[..., Any],
        step: QueryStepInput,
        *,
        day: int | None = None,
        submission_id: UUID | None = None,
    ) -> None:
        await workflow.execute_activity_method(
            activity,
            step.model_copy(update={"day": day, "submission_id": submission_id}),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )

    async def _run_stage(self, stage: Stage, submission_id: UUID | None = None) -> StageResult:
        await workflow.wait_condition(lambda: not self._held)
        assert self._claim_id is not None
        if submission_id is None and stage is Stage.CLASSIFICATION:
            submission_id = self._submission_id  # the claim's own submission, unless a reply
        try:
            return await workflow.execute_activity_method(
                STAGE_ACTIVITIES[stage],
                StageInput(claim_id=self._claim_id, stage=stage, submission_id=submission_id),
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=MODEL_RETRY if stage in MODEL_STAGES else RETRY,
            )
        except ActivityError as err:
            cause = err.cause
            if isinstance(cause, ApplicationError) and cause.type == MODEL_UNAVAILABLE:
                # Retries are used up. Record why, then fail the workflow.
                await self._event(stage, "stage_failed", {"error": cause.message})
            raise

    async def _corrected(
        self,
        stage: Stage,
        checkpoint: Checkpoint,
        outcome: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply a person's corrections as a new stage record. Corrections the service
        refuses send the same checkpoint back to the queue with the reason."""
        while outcome.get(CORRECTABLE[stage]):
            assert self._claim_id is not None
            try:
                await workflow.execute_activity_method(
                    ClaimActivities.apply_corrections,
                    CorrectionInput(
                        claim_id=self._claim_id,
                        stage=stage,
                        outcome=outcome,
                        actor=f"task:{self._last_task_id}",
                    ),
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=RETRY,
                )
                return outcome
            except ActivityError as err:
                reason = _reason(err)
                await self._event(stage, "corrections_rejected", {"reason": reason})
                outcome = await self._checkpoint(
                    stage, checkpoint, {**payload, "correction_error": reason}
                )
        return outcome

    async def _checkpoint(
        self,
        stage: Stage,
        checkpoint: Checkpoint,
        payload: dict[str, Any] | None = None,
        *,
        authority_amount: Decimal | None = None,
    ) -> dict[str, Any]:
        assert self._claim_id is not None
        task_id = await workflow.execute_activity_method(
            ClaimActivities.open_task,
            OpenTaskInput(
                claim_id=self._claim_id,
                workflow_id=workflow.info().workflow_id,
                stage=stage,
                kind=checkpoint.kind,
                role=checkpoint.role,
                title=checkpoint.title,
                payload={"stage": stage.value, **(payload or {})},
                deadline=workflow.now() + CHECKPOINT_SLA,
                authority_amount=authority_amount,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        key = self._last_task_id = str(task_id)
        try:
            await workflow.wait_condition(lambda: key in self._outcomes, timeout=CHECKPOINT_SLA)
        except TimeoutError:
            await self._event(
                stage, "checkpoint_escalated", {"task_id": key, "kind": checkpoint.kind}
            )
            await workflow.wait_condition(lambda: key in self._outcomes)
        outcome = self._outcomes[key]
        await self._event(
            stage, "checkpoint_completed", {"task_id": key, "kind": checkpoint.kind, **outcome}
        )
        return outcome

    async def _event(self, stage: Stage, action: str, detail: dict[str, Any] | None = None) -> None:
        assert self._claim_id is not None
        await workflow.execute_activity_method(
            ClaimActivities.record_event,
            EventInput(claim_id=self._claim_id, stage=stage, action=action, detail=detail),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
