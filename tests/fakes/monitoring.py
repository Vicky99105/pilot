from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.services.monitoring.contract import ClaimStatusRow
from app.shared.types import Stage


@dataclass
class RecordedEvent:
    claim_id: UUID
    stage: Stage
    action: str
    actor: str
    detail: dict[str, Any] | None


class FakeMonitoring:
    def __init__(self, rows: list[ClaimStatusRow] | None = None) -> None:
        self.events: list[RecordedEvent] = []
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
        self.events.append(RecordedEvent(claim_id, stage, action, actor, detail))

    async def claims_by_stage(self) -> list[ClaimStatusRow]:
        return self.rows

    def actions(self, stage: Stage) -> list[str]:
        return [e.action for e in self.events if e.stage == stage]
