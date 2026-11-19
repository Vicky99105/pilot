"""Contract for adjudication: draft a decision for the approver.

The model chooses the decision and writes the reasons; code checks them and sets the
amount. The amount is copied from the latest DeductionSheet.payable for approve and
partial, and is zero for query and reject. A code guardrail then re-reads the sheet and the
balance: a draft whose amount differs from the sheet, or exceeds the available balance,
is marked guardrail_passed=False and cannot be approved as it stands.

Callers import this module only.
"""

from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError

Decision = Literal["approve", "partial", "query", "reject"]


class Reason(BaseModel):
    text: str
    clause_ref: str | None = None
    rule_id: str | None = None
    line_id: int | None = None  # a DeductionSheet line


class DraftDecision(BaseModel):
    claim_id: UUID
    # The stage record versions the draft was built from; None when the stage has no record.
    input_versions: dict[str, int | None]
    decision: Decision
    expected_decision: Decision  # what the decision rules in code give
    amount: Decimal  # copied from the sheet; never computed by a model
    payable_on_sheet: Decimal
    available_balance: Decimal
    reasons: list[Reason]
    query_items: list[str]
    summary: str
    self_check_errors: list[str]  # left after one revision
    guardrail_errors: list[str]
    guardrail_passed: bool  # False when any self-check or guardrail error remains
    revisions: int


class NotReadyForAdjudication(PermanentError):
    """The claim has no registration, policy or deduction sheet yet."""


class AdjudicationService(Protocol):
    async def draft(self, claim_id: UUID) -> DraftDecision:
        """Idempotent per set of input record versions."""
        ...
