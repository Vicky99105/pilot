"""Contract for the claim event log and the claims-by-stage view."""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Stage


class ClaimStatusRow(BaseModel):
    claim_id: UUID
    claim_number: str | None
    state: str
    entered_at: datetime
    dwell_seconds: int
    deadline: datetime | None


class Monitoring(Protocol):
    async def record_event(
        self,
        *,
        claim_id: UUID,
        stage: Stage,
        action: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> None: ...

    async def claims_by_stage(self) -> list[ClaimStatusRow]: ...
