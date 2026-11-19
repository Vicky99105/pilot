"""Contract for reconsideration: the claimant asks for a closed claim to be decided again,
with grounds and new evidence.

The claim reopens (its history is kept: every stage record, event and letter stays). The new
evidence is a follow-up submission on the claim, and a new workflow run starts for it: it
classifies and reads the evidence into a new ClaimRecord version, then re-enters at
registration and runs every later stage as a new version. Settlement pays only what the
reconsidered decision adds to what the claim was paid before, and the letter says so.

Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.services.intake.contract import Channel, UploadedFile
from app.shared.errors import PermanentError

MIN_GROUNDS = 10  # characters: the grounds must say something


class Reconsideration(BaseModel):
    reconsideration_id: UUID
    claim_id: UUID
    number: int  # 1 for the claim's first reconsideration
    submission_id: UUID  # the new evidence
    workflow_id: str  # the run that decides it again
    grounds: str
    requested_by: UUID
    requested_at: datetime
    previous_decision: str | None  # approve, partial or reject
    previous_amount: Decimal  # the decided amount before
    previously_paid: Decimal  # what settlements paid before
    previous_closed_at: datetime | None


class NotReconsiderable(PermanentError):
    """The claim is unknown or not closed, the grounds are too short, or there is no new
    evidence."""


class ReconsiderationService(Protocol):
    async def request(
        self,
        claim_id: UUID,
        *,
        grounds: str,
        submitted_by: UUID,
        channel: Channel,
        files: list[UploadedFile],
    ) -> Reconsideration: ...

    async def history(self, claim_id: UUID) -> list[Reconsideration]:
        """The claim's reconsiderations, oldest first."""
        ...
