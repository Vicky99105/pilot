"""Contract for the cashless requests on one admission: the pre-auth that opens the claim,
then enhancements, the discharge or a cancellation, each a follow-up on the same claim.

- begin_request makes a submission the claim's current request. Its clock (set at intake:
  1 hour for a pre-auth or enhancement, 3 hours for a discharge) becomes the claim's
  deadline, so the queues order its tasks by time left.
- authorise runs after the decision of a request. A pre-auth or enhancement sets the sum
  insured block to the approved total (the block an enhancement raises); a rejected pre-auth
  releases the block and closes the claim. The discharge's final authorisation leaves the
  block for settlement to release, and starts the 15-day courier deadline for the originals.
- cancel releases the block and closes the claim.

Every amount comes from the decision record. Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.services.decision.contract import FinalDecision
from app.shared.errors import PermanentError
from app.shared.types import RequestType

ORIGINALS_COURIER_DAYS = 15  # the hospital couriers the originals within this many days
AWAITING_DISCHARGE = "awaiting_discharge"  # claim status between requests

AuthorisationStatus = Literal["awaiting_discharge", "final", "rejected", "cancelled"]


class CashlessRequest(BaseModel):
    claim_id: UUID
    submission_id: UUID
    request_type: RequestType
    received_at: datetime
    clock_deadline: datetime | None  # None for a cancellation, which has no clock


class Authorisation(BaseModel):
    claim_id: UUID
    request_type: RequestType  # the request this authorises
    submission_id: UUID | None
    decision_version: int | None  # None for a cancellation
    final_decision: FinalDecision | None  # None for a cancellation
    approved_now: Decimal  # this request's change to the approved total
    previous_total: Decimal  # approved before this request
    approved_total: Decimal  # approved after it: the pre-auth plus every enhancement
    # The same, before co-pay: the admissible total the decision paid. R26 holds the final
    # bill's admissible total to it, so co-pay is not taken twice.
    approved_admissible: Decimal
    diagnosis_codes: list[str]  # verified at the pre-auth; R26 compares the final bill's
    block_id: str | None
    blocked: Decimal  # the sum insured block after this request
    status: AuthorisationStatus
    originals_due_at: datetime | None = None  # discharge: the courier deadline
    authorised_at: datetime


class OriginalsStatus(BaseModel):
    claim_id: UUID
    due_at: datetime
    received_at: datetime | None
    received_by: UUID | None


class NotCashless(PermanentError):
    """The claim was not opened by a pre-auth, or the submission is not on the claim."""


class NotReadyToAuthorise(PermanentError):
    """No final decision yet, or a partial or reject the CRC has not upheld."""


class NoOriginalsDue(PermanentError):
    """The claim has no discharge authorisation, so no originals are due."""


class CashlessService(Protocol):
    async def begin_request(self, claim_id: UUID, submission_id: UUID) -> CashlessRequest: ...

    async def authorise(self, claim_id: UUID) -> Authorisation:
        """Idempotent per decision version."""
        ...

    async def latest_authorisation(self, claim_id: UUID) -> Authorisation | None: ...

    async def mark_awaiting_discharge(self, claim_id: UUID) -> None:
        """Status awaiting_discharge: the claim waits for the hospital's next request."""
        ...

    async def cancel(self, claim_id: UUID, submission_id: UUID) -> Authorisation:
        """Release the block and close the claim. Idempotent."""
        ...

    async def mark_originals_received(self, claim_id: UUID, user_id: UUID) -> OriginalsStatus: ...
