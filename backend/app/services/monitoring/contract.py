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


# --- Pilot metrics (P3-4) -----------------------------------------------------------------
# Every metric counts what happened inside the period [start, end). Percentiles interpolate
# linearly between the two nearest values (as Postgres percentile_cont does). A rate is None
# when its base is zero. Times are seconds unless a name says otherwise.


class TaskTurnaround(BaseModel):
    key: str  # a queue (role), or the stage that opened the checkpoint
    completed: int  # tasks completed in the period
    p50_seconds: float | None  # task opened to completed
    p95_seconds: float | None
    late: int  # completed after the task's deadline


class RequestTurnaround(BaseModel):
    """End-to-end turnaround by request type. A reimbursement runs from the claim's first
    event to claim_closed; a reconsideration from reconsideration_requested to the next
    claim_closed; a cashless request (preauth, enhancement, discharge) from request_started
    to the first communication stage_completed after it, when its clock stops."""

    request_type: str
    answered: int  # closed or answered in the period
    p50_seconds: float | None
    p95_seconds: float | None
    # Cashless: clock_breached events in the period. Reimbursement and reconsideration:
    # claims closed after the claim's deadline.
    breaches: int


class QueryReason(BaseModel):
    reason: str  # the missing document type, else the item's source (medical, adjudication)
    claims: int  # claims received in the period with a query item of this reason
    rate: float | None  # claims / claims received


class QueryRate(BaseModel):
    claims_received: int  # claims created in the period
    claims_queried: int  # of those, claims with at least one query
    rate: float | None
    by_reason: list[QueryReason]  # most claims first


class OverrideRate(BaseModel):
    """A decision is a completed task of an overridable kind, counted once per claim and
    stage (and user, per user). It is overridden when the override log has an entry for
    the same claim and stage (and user) in the period."""

    key: str  # a stage, or a user ID
    name: str | None = None  # the user's name, per user
    decided: int
    overridden: int
    rate: float | None  # overridden / decided
    entries: int  # override log entries in the period (one per field changed)


class Outcomes(BaseModel):
    decided: int  # claims with a final decision in the period; the latest one counts
    by_decision: dict[str, int]  # approve, partial, reject
    referred_to_crc: int  # claims with a CRC task opened in the period
    risk_scored: int  # claims the risk stage scored in the period; the latest score counts
    risk_referred: int  # of those, claims referred to an investigator
    risk_referral_rate: float | None
    reconsiderations: int  # reconsiderations requested in the period


class ModelUse(BaseModel):
    role: str  # the model role, e.g. classifier, adjudicator
    calls: int
    claims: int  # distinct claims among the calls
    invalid: int  # calls whose output failed validation
    total_cost: float  # every call with a known cost
    cost_per_claim: float | None  # cost of calls on a claim / claims
    latency_p50_ms: float | None  # per call
    latency_p95_ms: float | None


class ModelCosts(BaseModel):
    calls: int
    claims: int
    total_cost: float
    cost_per_claim_mean: float | None  # over claims with at least one call
    cost_per_claim_p50: float | None
    cost_per_claim_p95: float | None
    latency_per_claim_p50_ms: float | None  # the sum of a claim's call latencies
    latency_per_claim_p95_ms: float | None
    by_role: list[ModelUse]


class PilotMetrics(BaseModel):
    start: datetime
    end: datetime
    generated_at: datetime
    turnaround_by_role: list[TaskTurnaround]
    turnaround_by_stage: list[TaskTurnaround]
    request_turnaround: list[RequestTurnaround]  # every request type, empty ones included
    clock_breaches: int  # cashless clock_breached events in the period
    queries: QueryRate
    overrides_by_stage: list[OverrideRate]
    overrides_by_user: list[OverrideRate]
    outcomes: Outcomes
    models: ModelCosts


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

    async def metrics(self, *, start: datetime, end: datetime) -> PilotMetrics:
        """Pilot metrics over [start, end), computed from tasks, events, queries, the
        override log, reconsiderations and model calls. No model is called."""
        ...
