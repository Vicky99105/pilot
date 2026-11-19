"""Inputs and outputs of ClaimWorkflow and its activities."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import RequestType, Role, Stage

DraftDecision = Literal["approve", "partial", "query", "reject"]


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


class OpenTaskInput(BaseModel):
    claim_id: UUID
    workflow_id: str
    stage: Stage
    kind: str
    role: Role
    title: str
    payload: dict[str, Any]
    deadline: datetime


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


class ClaimResult(BaseModel):
    claim_id: UUID
    status: Literal["closed"]
    draft: DraftDecision | None
