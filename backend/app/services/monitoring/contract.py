"""Contract for the claim event log and the claims-by-stage view."""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Stage

# The claim ID for events about no single claim, such as a test-data reset of a mock core
# policy. No claim has it, so these events never move a claim's status.
SYSTEM_EVENTS = UUID(int=0)


class ClaimStatusRow(BaseModel):
    claim_id: UUID
    claim_number: str | None
    state: str
    entered_at: datetime
    dwell_seconds: int
    deadline: datetime | None


class ClaimEvent(BaseModel):
    stage: Stage
    action: str
    actor: str
    detail: dict[str, Any] | None
    created_at: datetime


class ClaimTurnaround(BaseModel):
    claim_id: UUID
    status: str
    received_at: datetime  # the claim's first event
    closed_at: datetime | None
    turnaround_seconds: int  # received to closed, or to now while open
    deadline: datetime | None
    within_deadline: bool | None  # None when the claim has no deadline
    checkpoints: int  # checkpoint tasks opened


class Monitoring(Protocol):
    async def record_event(
        self,
        *,
        claim_id: UUID,
        stage: Stage,
        action: str,
        actor: str,
        detail: dict[str, Any] | None = None,
        moves_status: bool = True,
    ) -> None:
        """Append the event. With moves_status, the claim's status becomes the event's stage
        (a closed claim stays closed). Events about the claim rather than a stage (a hold, a
        paused clock, a reassignment) pass moves_status=False."""
        ...

    async def claims_by_stage(self) -> list[ClaimStatusRow]: ...

    async def events(self, claim_id: UUID) -> list[ClaimEvent]: ...  # oldest first

    async def summarise(self, claim_id: UUID) -> ClaimTurnaround: ...
