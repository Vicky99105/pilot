"""Contract for the decision stage: route the adjudication draft to an approver by amount,
record the approver's action, and record the CRC review of a partial or reject.

The amount at stake is the payable on the latest deduction sheet. The authority matrix
(config/authority_matrix.yaml) names the band; a user may act when the max_amount on their
approver role covers that amount, or is empty (no limit).

Every record() and review_crc() call appends a new DecisionRecord version, so send-backs
keep their history. Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.services.adjudication.contract import Decision
from app.shared.errors import PermanentError
from app.shared.types import Role, Stage

ApproverAction = Literal["approve", "override", "send_back"]
CrcAction = Literal["uphold", "send_back"]
FinalDecision = Literal["approve", "partial", "reject"]
PAID: tuple[FinalDecision, ...] = ("approve", "partial")


class Routing(BaseModel):
    claim_id: UUID
    adjudication_version: int
    draft_decision: Decision
    draft_guardrail_passed: bool
    amount: Decimal  # the payable on the sheet: what approving would pay
    band: str  # a band name from the authority matrix
    role: Role
    authority_amount: Decimal  # the approver's max_amount must cover this
    crc_required: bool  # for the draft decision; the final decision decides in the end


def _rupees(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.01")) if value is not None else None


class ApproverOutcome(BaseModel):
    """The approver's checkpoint outcome."""

    action: ApproverAction
    approver_id: UUID
    task_id: UUID | None = None  # the checkpoint task; a repeat with the same task is a no-op
    decision: FinalDecision | None = None  # override: the final decision
    amount: Decimal | None = None  # override: the amount to pay
    reason: str = ""  # required for an override
    stage: Stage | None = None  # send_back: the stage to re-run
    note: str = ""  # required for a send-back

    _quantize = field_validator("amount")(_rupees)


class CrcOutcome(BaseModel):
    action: CrcAction
    reviewer_id: UUID
    task_id: UUID | None = None
    stage: Stage | None = None  # send_back: the stage to re-run
    note: str = ""  # required for a send-back


class Override(BaseModel):
    field: Literal["decision", "amount"]
    before: str
    after: str


class CrcReview(BaseModel):
    reviewer_id: UUID
    task_id: UUID | None
    action: CrcAction
    send_back_stage: Stage | None
    note: str
    reviewed_at: datetime


class DecisionRecord(BaseModel):
    claim_id: UUID
    routing: Routing
    action: ApproverAction
    approver_id: UUID
    task_id: UUID | None
    final_decision: FinalDecision | None  # None after a send-back
    final_amount: Decimal  # zero for a reject or a send-back
    overrides: list[Override]
    reason: str
    send_back_stage: Stage | None
    send_back_note: str
    crc_required: bool  # the final decision is one the CRC must approve
    crc: CrcReview | None = None
    decided_at: datetime


class NotReadyForDecision(PermanentError):
    """The claim has no adjudication draft or deduction sheet."""


class InvalidDecision(PermanentError):
    """The outcome does not fit the draft: a missing reason, an amount above the sheet, a
    send-back to a later stage, or a CRC review where none is due."""


class NotAuthorised(InvalidDecision):
    """The user lacks the role, or their limit does not cover the amount."""


class DecisionService(Protocol):
    async def route(self, claim_id: UUID) -> Routing: ...

    async def record(self, claim_id: UUID, outcome: ApproverOutcome) -> DecisionRecord:
        """Validate the approver's action against the latest draft and store it."""
        ...

    async def review_crc(self, claim_id: UUID, outcome: CrcOutcome) -> DecisionRecord:
        """The CRC review of the latest record, which must be a partial or a reject."""
        ...

    async def latest(self, claim_id: UUID) -> DecisionRecord | None: ...
