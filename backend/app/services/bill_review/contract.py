"""Contract for bill review: map each bill line to a standard head, apply the bill rules
and produce the deduction sheet.

Rules run in this order: R11 (non-payable items) per line, R13 (room cap and
proportionate deduction) per line, then R21 (co-pay) on the admissible total. Every
amount is Decimal rupees, quantized to 2 places.

Callers import this module only.
"""

from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.services.rules.contract import Head
from app.shared.errors import PermanentError
from app.shared.types import RuleResult


class DeductionLine(BaseModel):
    line_id: int  # the bill line's ID in the ClaimRecord
    head: Head
    head_as_written: str | None  # the bill's section heading
    description: str
    claimed: Decimal
    admissible: Decimal
    deducted: Decimal  # claimed - admissible
    rule_id: str | None  # the rule that set the admissible amount, if any
    clause_ref: str | None
    reason: str | None
    judged_by: Literal["code", "model", "person"] = "code"
    flagged: bool = False  # True when the line could not be mapped to a head


class DeductionSheet(BaseModel):
    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version the sheet was built from
    registration_version: int  # the RegistrationResult version it used
    product_uin: str
    lines: list[DeductionLine]
    rule_results: list[RuleResult]  # every rule result, per line and for the claim
    claimed_total: Decimal
    admissible_total: Decimal  # before co-pay
    co_pay: Decimal  # positive: the amount the insured person bears
    payable: Decimal  # admissible_total - co_pay
    flagged_lines: list[int]  # line IDs mapped to "other" for want of a known head


class LineEdit(BaseModel):
    """A person's change to one line at "confirm deductions"."""

    line_id: int
    admissible: Decimal
    reason: str

    @field_validator("admissible")
    @classmethod
    def _rupees(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class NotReadyForBillReview(PermanentError):
    """The claim has no extracted record or no registration yet."""


class InvalidEdit(PermanentError):
    """An edit names an unknown line, has no reason, or sets an amount outside 0..claimed."""


class BillReviewService(Protocol):
    async def review(self, claim_id: UUID) -> DeductionSheet:
        """Idempotent per (ClaimRecord version, RegistrationResult version)."""
        ...

    async def apply_edits(
        self, claim_id: UUID, edits: list[LineEdit], *, actor: str
    ) -> DeductionSheet:
        """Store a person's line changes as a new version: judged_by="person", the
        co-pay and payable recomputed."""
        ...
