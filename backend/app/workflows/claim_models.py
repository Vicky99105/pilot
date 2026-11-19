"""Inputs and outputs of ClaimWorkflow and its activities."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.shared.types import RequestType, Role, Stage

DraftDecision = Literal["approve", "partial", "query", "reject"]
FinalDecision = Literal["approve", "partial", "reject"]


class ClaimInput(BaseModel):
    submission_id: UUID
    request_type: RequestType


class StageInput(BaseModel):
    claim_id: UUID
    stage: Stage
    submission_id: UUID | None = None  # set when a follow-up submission triggers a re-run


class StageResult(BaseModel):
    claim_id: UUID
    stage: Stage
    needs_checkpoint: bool = False  # conditional checkpoints: unsure labels, adverse findings
    checkpoint_payload: dict[str, Any] = {}  # what the person needs to see at the checkpoint
    draft: DraftDecision | None = None  # adjudication only
    authority_amount: Decimal | None = None  # decision only: the approver limit needed


class OpenTaskInput(BaseModel):
    claim_id: UUID
    workflow_id: str
    stage: Stage
    kind: str
    role: Role
    title: str
    payload: dict[str, Any]
    deadline: datetime
    authority_amount: Decimal | None = None


class EventInput(BaseModel):
    claim_id: UUID
    stage: Stage
    action: str
    actor: str = "workflow"
    detail: dict[str, Any] | None = None


class CorrectionInput(BaseModel):
    """A checkpoint outcome carrying a person's corrections for the stage's record."""

    claim_id: UUID
    stage: Stage
    outcome: dict[str, Any]
    actor: str


class OutcomeInput(BaseModel):
    """A checkpoint outcome for an activity that records it: the approver's decision, the
    CRC review or the finance release. outcome["completed_by"] names the person."""

    claim_id: UUID
    stage: Stage
    task_id: UUID
    outcome: dict[str, Any]


class OverruleOutcome(BaseModel):
    """The medical officer's overrule: the findings that do not hold, and why."""

    action: Literal["overrule"]
    finding_ids: list[int] = Field(min_length=1)
    reason: str = Field(min_length=1)


class VerdictOutcome(BaseModel):
    """The investigator's verdict on a referred claim, with what they found."""

    action: Literal["cleared", "fraud_confirmed"]
    note: str = Field(min_length=1)


class DecisionStep(BaseModel):
    final_decision: FinalDecision | None  # None after a send-back
    crc_required: bool
    send_back_stage: Stage | None


class QueryRaised(BaseModel):
    query_id: UUID | None  # None when there was nothing new to ask
    reply_window_days: int = 0
    reminder_days: list[int] = []


class QueryStepInput(BaseModel):
    claim_id: UUID
    query_id: UUID
    day: int | None = None  # reminders
    submission_id: UUID | None = None  # replies


class ClaimResult(BaseModel):
    claim_id: UUID
    status: Literal["closed"]
    draft: DraftDecision | None  # the final decision
