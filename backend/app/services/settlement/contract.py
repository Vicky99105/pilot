"""Contract for settlement. The order is fixed:

1. check_payee: IFSC format, the account holder against the member, the cheque against
   the claim form Part A. Nothing is paid.
2. A person with the FINANCE role releases the payment (a workflow checkpoint).
3. settle: the payment instruction (idempotency key claim_id + "pay" + decision version),
   then the sum insured block released with utilised = the paid amount, then the claim
   closes. A rejected claim needs no release: its whole block is released and it closes.

Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.integrations.core.contract import Payee, PaymentRef
from app.services.decision.contract import FinalDecision
from app.shared.errors import PermanentError

IFSC_PATTERN = r"^[A-Z]{4}0[A-Z0-9]{6}$"
HOLDER_MATCH_SCORE = 85  # rapidfuzz token sort ratio, 0 to 100

PayeeCheckId = Literal["ifsc_format", "holder_matches_member", "cheque_matches_claim_form"]


class PayeeCheck(BaseModel):
    check_id: PayeeCheckId
    passed: bool
    detail: str


class SettlementRecord(BaseModel):
    claim_id: UUID
    decision_version: int
    final_decision: FinalDecision
    paid_amount: Decimal  # zero for a reject
    payee: Payee | None  # None for a reject
    payee_checks: list[PayeeCheck]
    status: Literal["awaiting_release", "settled"]
    released_by: UUID | None  # the finance user who released the payment
    payment_ref: PaymentRef | None
    block_id: str | None
    utilised: Decimal  # charged to the sum insured
    released: Decimal  # returned to the sum insured from the block
    closed_at: datetime | None


class NotReadyForSettlement(PermanentError):
    """No final decision, or the decision is not ready to be paid."""


class PaymentNotReleased(PermanentError):
    """A paid claim needs a release by a user with the FINANCE role first."""


class PayeeCheckFailed(PermanentError):
    """A payee check failed, so no payment instruction is created."""


class SettlementService(Protocol):
    async def check_payee(self, claim_id: UUID) -> SettlementRecord: ...

    async def settle(self, claim_id: UUID, *, released_by: UUID | None) -> SettlementRecord:
        """Idempotent per decision version: a repeat returns the settled record."""
        ...
