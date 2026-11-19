from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.monitoring.contract import ClaimEvent, ClaimStatusRow, ClaimTurnaround
from app.shared.types import Stage


@dataclass
class RecordedEvent:
    claim_id: UUID
    stage: Stage
    action: str
    actor: str
    detail: dict[str, Any] | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeMonitoring:
    """Implements the Monitoring contract in memory. recorded holds every event."""

    def __init__(self, rows: list[ClaimStatusRow] | None = None) -> None:
        self.recorded: list[RecordedEvent] = []
        self.rows = rows or []

    async def record_event(
        self,
        *,
        claim_id: UUID,
        stage: Stage,
        action: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.recorded.append(RecordedEvent(claim_id, stage, action, actor, detail))

    async def claims_by_stage(self) -> list[ClaimStatusRow]:
        return self.rows

    async def events(self, claim_id: UUID) -> list[ClaimEvent]:
        return [
            ClaimEvent(
                stage=e.stage,
                action=e.action,
                actor=e.actor,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in self.recorded
            if e.claim_id == claim_id
        ]

    async def summarise(self, claim_id: UUID) -> ClaimTurnaround:
        mine = [e for e in self.recorded if e.claim_id == claim_id]
        received = mine[0].created_at if mine else datetime.now(UTC)
        return ClaimTurnaround(
            claim_id=claim_id,
            status="closed",
            received_at=received,
            closed_at=None,
            turnaround_seconds=0,
            deadline=None,
            within_deadline=None,
            checkpoints=sum(1 for e in mine if e.action == "checkpoint_opened"),
        )

    def actions(self, stage: Stage) -> list[str]:
        return [e.action for e in self.recorded if e.stage == stage]
