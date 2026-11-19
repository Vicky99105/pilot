from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.monitoring.contract import (
    ClaimEvent,
    ClaimStatusRow,
    ClaimTurnaround,
    ModelCosts,
    Outcomes,
    PilotMetrics,
    QueryRate,
)
from app.shared.types import Stage


@dataclass
class RecordedEvent:
    claim_id: UUID
    stage: Stage
    action: str
    actor: str
    detail: dict[str, Any] | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    moves_status: bool = True


class FakeMonitoring:
    """Implements the Monitoring contract in memory. recorded holds every event."""

    def __init__(self, rows: list[ClaimStatusRow] | None = None) -> None:
        self.recorded: list[RecordedEvent] = []
        self.rows = rows or []
        self.metrics_asked: list[tuple[datetime, datetime]] = []

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
        self.recorded.append(
            RecordedEvent(claim_id, stage, action, actor, detail, moves_status=moves_status)
        )

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

    async def metrics(self, *, start: datetime, end: datetime) -> PilotMetrics:
        """An empty period; metrics_asked records each period asked for."""
        self.metrics_asked.append((start, end))
        return PilotMetrics(
            start=start,
            end=end,
            generated_at=end,
            turnaround_by_role=[],
            turnaround_by_stage=[],
            request_turnaround=[],
            clock_breaches=0,
            queries=QueryRate(claims_received=0, claims_queried=0, rate=None, by_reason=[]),
            overrides_by_stage=[],
            overrides_by_user=[],
            outcomes=Outcomes(decided=0, by_decision={}, referred_to_crc=0, risk_scored=0,
                              risk_referred=0, risk_referral_rate=None, reconsiderations=0),
            models=ModelCosts(calls=0, claims=0, total_cost=0, cost_per_claim_mean=None,
                              cost_per_claim_p50=None, cost_per_claim_p95=None,
                              latency_per_claim_p50_ms=None, latency_per_claim_p95_ms=None,
                              by_role=[]),
        )  # fmt: skip

    def actions(self, stage: Stage) -> list[str]:
        return [e.action for e in self.recorded if e.stage == stage]
