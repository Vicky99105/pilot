"""ClaimWorkflow: one workflow per claim, through all 13 stages with human checkpoints.

Checkpoint pattern: the open_task activity, then wait for that task's outcome through the
task_completed signal. When the deadline passes, an escalation event is recorded and the
workflow keeps waiting. A send-back outcome re-runs the named stage.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.shared.types import Role, Stage
    from app.workflows.claim_activities import ClaimActivities
    from app.workflows.claim_models import (
        ClaimInput,
        ClaimResult,
        DraftDecision,
        EventInput,
        OpenTaskInput,
        StageInput,
        StageResult,
    )

CHECKPOINT_SLA = timedelta(hours=4)
QUERY_REPLY_WINDOW = timedelta(days=15)
ACTIVITY_TIMEOUT = timedelta(minutes=5)
RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["PermanentError"])

# Stages after intake, in order. Query and monitoring run outside this list.
PIPELINE = [
    Stage.CLASSIFICATION,
    Stage.DIGITISATION,
    Stage.REGISTRATION,
    Stage.BILL_REVIEW,
    Stage.MEDICAL_REVIEW,
    Stage.RISK,
    Stage.ADJUDICATION,
    Stage.DECISION,
    Stage.COMMUNICATION,
    Stage.SETTLEMENT,
]

STAGE_ACTIVITIES = {
    Stage.CLASSIFICATION: ClaimActivities.classification,
    Stage.DIGITISATION: ClaimActivities.digitisation,
    Stage.REGISTRATION: ClaimActivities.registration,
    Stage.BILL_REVIEW: ClaimActivities.bill_review,
    Stage.MEDICAL_REVIEW: ClaimActivities.medical_review,
    Stage.RISK: ClaimActivities.risk,
    Stage.ADJUDICATION: ClaimActivities.adjudication,
    Stage.DECISION: ClaimActivities.decision,
    Stage.COMMUNICATION: ClaimActivities.communication,
    Stage.QUERY: ClaimActivities.query,
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

CONDITIONAL = {
    Stage.CLASSIFICATION: FIX_LABELS,
    Stage.DIGITISATION: FIX_FIELDS,
    Stage.MEDICAL_REVIEW: MEDICAL_FINDINGS,
    Stage.RISK: INVESTIGATE,
}


def checkpoints_after(
    stage: Stage, result: StageResult, draft: DraftDecision | None
) -> list[Checkpoint]:
    if stage in CONDITIONAL:
        return [CONDITIONAL[stage]] if result.needs_checkpoint else []
    if stage is Stage.REGISTRATION:
        return [CONFIRM_REGISTRATION]
    if stage is Stage.BILL_REVIEW:
        return [CONFIRM_DEDUCTIONS]
    if stage is Stage.DECISION:
        return [APPROVE, CRC] if draft in ("partial", "reject") else [APPROVE]
    if stage is Stage.SETTLEMENT and draft != "reject":
        return [RELEASE_PAYMENT]
    return []


def send_back_target(outcome: dict[str, Any], reachable: list[Stage]) -> Stage | None:
    """The stage a send-back outcome names, if it is this stage or an earlier one."""
    if outcome.get("action") != "send_back":
        return None
    target = outcome.get("stage")
    return next((s for s in reachable if s.value == target), None)


@workflow.defn
class ClaimWorkflow:
    def __init__(self) -> None:
        self._outcomes: dict[str, dict[str, Any]] = {}
        self._followups: list[str] = []
        self._held = False
        self._claim_id: UUID | None = None
        self._draft: DraftDecision | None = None

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
            result = await self._run_stage(stage)
            if stage is Stage.ADJUDICATION:
                self._draft = result.draft
                if result.draft == "query":
                    await self._query_loop()
                    continue  # resume at the stage that raised the query
            target = None
            for checkpoint in checkpoints_after(stage, result, self._draft):
                outcome = await self._checkpoint(stage, checkpoint)
                target = send_back_target(outcome, PIPELINE[: i + 1])
                if target is not None:
                    break
            if target is not None:
                await self._event(stage, "sent_back", {"to": target.value})
                i = PIPELINE.index(target)
                continue
            i += 1
        await self._run_stage(Stage.MONITORING)
        return ClaimResult(claim_id=claim_id, status="closed", draft=self._draft)

    async def _run_stage(self, stage: Stage, submission_id: str | None = None) -> StageResult:
        await workflow.wait_condition(lambda: not self._held)
        assert self._claim_id is not None
        return await workflow.execute_activity_method(
            STAGE_ACTIVITIES[stage],
            StageInput(
                claim_id=self._claim_id,
                stage=stage,
                submission_id=UUID(submission_id) if submission_id else None,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )

    async def _query_loop(self) -> None:
        seen = len(self._followups)
        await self._run_stage(Stage.QUERY)
        try:
            await workflow.wait_condition(
                lambda: len(self._followups) > seen, timeout=QUERY_REPLY_WINDOW
            )
        except TimeoutError:
            await self._event(Stage.QUERY, "query_deadline_passed")
            await self._checkpoint(Stage.QUERY, QUERY_NO_REPLY)
            return
        reply = self._followups[seen]
        await self._event(Stage.QUERY, "followup_received", {"submission_id": reply})
        await self._run_stage(Stage.CLASSIFICATION, submission_id=reply)
        await self._run_stage(Stage.DIGITISATION, submission_id=reply)

    async def _checkpoint(self, stage: Stage, checkpoint: Checkpoint) -> dict[str, Any]:
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
                payload={"stage": stage.value},
                deadline=workflow.now() + CHECKPOINT_SLA,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        key = str(task_id)
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
