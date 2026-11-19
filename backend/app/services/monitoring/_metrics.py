"""Pilot metrics for DefaultMonitoring. SQL selects the rows of the period; the grouping,
pairing and percentiles run here in code so the same logic works on Postgres and SQLite.
Internal to the monitoring module: callers use Monitoring.metrics."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ClaimRow,
    EventRow,
    ModelCallRow,
    OverrideRow,
    QueryRow,
    ReconsiderationRow,
    TaskRow,
    UserRow,
)
from app.services.monitoring.contract import (
    ModelCosts,
    ModelUse,
    Outcomes,
    OverrideRate,
    PilotMetrics,
    QueryRate,
    QueryReason,
    RequestTurnaround,
    TaskTurnaround,
)
from app.services.supervision.contract import OVERRIDABLE_KINDS
from app.shared.types import RequestType, Stage

CASHLESS_TYPES = (RequestType.PREAUTH, RequestType.ENHANCEMENT, RequestType.DISCHARGE)
REQUEST_TYPES = (
    RequestType.REIMBURSEMENT,
    *CASHLESS_TYPES,
    RequestType.RECONSIDERATION,
)
UNKNOWN_STAGE = "unknown"


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between the closest ranks, as percentile_cont does."""
    if not values:
        return None
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 6)


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def pilot_metrics(
    session: AsyncSession, start: datetime, end: datetime, now: datetime
) -> PilotMetrics:
    tasks = list(
        await session.scalars(
            select(TaskRow).where(
                TaskRow.status == "done", TaskRow.completed_at >= start, TaskRow.completed_at < end
            )
        )
    )
    request_rows = await _request_turnaround(session, start, end)
    return PilotMetrics(
        start=start,
        end=end,
        generated_at=now,
        turnaround_by_role=_task_turnaround(tasks, lambda t: t.role),
        turnaround_by_stage=_task_turnaround(tasks, _stage_of),
        request_turnaround=request_rows,
        clock_breaches=sum(
            r.breaches for r in request_rows if r.request_type in set(CASHLESS_TYPES)
        ),
        queries=await _query_rate(session, start, end),
        overrides_by_stage=await _overrides(session, tasks, start, end, per_user=False),
        overrides_by_user=await _overrides(session, tasks, start, end, per_user=True),
        outcomes=await _outcomes(session, start, end),
        models=await _models(session, start, end),
    )


# --- Turnaround --------------------------------------------------------------------------


def _stage_of(task: TaskRow) -> str:
    stage = (task.payload or {}).get("stage")
    return str(stage) if stage else UNKNOWN_STAGE


def _seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def _task_turnaround(tasks: list[TaskRow], key: Any) -> list[TaskTurnaround]:
    groups: dict[str, list[TaskRow]] = defaultdict(list)
    for task in tasks:
        groups[key(task)].append(task)
    found = []
    for name, group in groups.items():
        times = [_seconds(t.created_at, t.completed_at) for t in group if t.completed_at]
        found.append(
            TaskTurnaround(
                key=name,
                completed=len(group),
                p50_seconds=percentile(times, 0.5),
                p95_seconds=percentile(times, 0.95),
                late=sum(1 for t in group if t.completed_at and t.completed_at > t.deadline),
            )
        )
    return sorted(found, key=lambda t: (-t.completed, t.key))


async def _events(
    session: AsyncSession, *conditions: Any, claim_ids: Iterable[UUID] | None = None
) -> list[EventRow]:
    query = select(EventRow).where(*conditions)
    if claim_ids is not None:
        query = query.where(EventRow.claim_id.in_(set(claim_ids)))
    return list(await session.scalars(query.order_by(EventRow.created_at, EventRow.id)))


async def _request_turnaround(
    session: AsyncSession, start: datetime, end: datetime
) -> list[RequestTurnaround]:
    times: dict[str, list[float]] = defaultdict(list)
    breaches: Counter[str] = Counter()

    # Reimbursement and reconsideration: claim_closed events in the period.
    closed = await _events(
        session,
        EventRow.action == "claim_closed",
        EventRow.created_at >= start,
        EventRow.created_at < end,
    )
    claims = {
        c.id: c
        for c in await session.scalars(
            select(ClaimRow).where(ClaimRow.id.in_({e.claim_id for e in closed}))
        )
    }
    closed = [
        e
        for e in closed
        if e.claim_id in claims and claims[e.claim_id].request_type != RequestType.PREAUTH.value
    ]
    history = defaultdict(list)
    for event in await _events(
        session, EventRow.created_at < end, claim_ids={e.claim_id for e in closed}
    ):
        history[event.claim_id].append(event)
    for close in closed:
        before = [e for e in history[close.claim_id] if e.created_at < close.created_at]
        if not before:
            continue
        reconsidered = [e for e in before if e.action == "reconsideration_requested"]
        if reconsidered:
            kind, began = RequestType.RECONSIDERATION.value, reconsidered[-1].created_at
        else:
            kind, began = RequestType.REIMBURSEMENT.value, before[0].created_at
        times[kind].append(_seconds(began, close.created_at))
        deadline = claims[close.claim_id].deadline
        if deadline is not None and close.created_at > deadline:
            breaches[kind] += 1

    # Cashless: each request_started paired with the first communication after it.
    answered = await _events(
        session,
        EventRow.stage == Stage.COMMUNICATION.value,
        EventRow.action == "stage_completed",
        EventRow.created_at >= start,
        EventRow.created_at < end,
    )
    steps = await _events(
        session,
        EventRow.created_at < end,
        EventRow.action.in_(("request_started", "stage_completed")),
        claim_ids={e.claim_id for e in answered},
    )
    pending: dict[UUID, EventRow] = {}
    for event in steps:
        if event.action == "request_started":
            pending[event.claim_id] = event
        elif event.stage == Stage.COMMUNICATION.value and event.claim_id in pending:
            request = pending.pop(event.claim_id)
            if event.created_at >= start:
                kind = str((request.detail or {}).get("request_type", ""))
                times[kind].append(_seconds(request.created_at, event.created_at))
    for event in await _events(
        session,
        EventRow.action == "clock_breached",
        EventRow.created_at >= start,
        EventRow.created_at < end,
    ):
        breaches[str((event.detail or {}).get("request_type", ""))] += 1

    return [
        RequestTurnaround(
            request_type=kind.value,
            answered=len(times[kind.value]),
            p50_seconds=percentile(times[kind.value], 0.5),
            p95_seconds=percentile(times[kind.value], 0.95),
            breaches=breaches[kind.value],
        )
        for kind in REQUEST_TYPES
    ]


# --- Queries -----------------------------------------------------------------------------


async def _query_rate(session: AsyncSession, start: datetime, end: datetime) -> QueryRate:
    received = await session.scalar(
        select(func.count())
        .select_from(ClaimRow)
        .where(ClaimRow.created_at >= start, ClaimRow.created_at < end)
    )
    queries = await session.execute(
        select(QueryRow.claim_id, QueryRow.items)
        .join(ClaimRow, ClaimRow.id == QueryRow.claim_id)
        .where(ClaimRow.created_at >= start, ClaimRow.created_at < end)
    )
    reasons: dict[str, set[UUID]] = defaultdict(set)
    queried: set[UUID] = set()
    for claim_id, items in queries:
        queried.add(claim_id)
        for item in items or []:
            reasons[str(item.get("doc_type") or item.get("source") or "other")].add(claim_id)
    total = received or 0
    return QueryRate(
        claims_received=total,
        claims_queried=len(queried),
        rate=rate(len(queried), total),
        by_reason=sorted(
            (
                QueryReason(reason=reason, claims=len(ids), rate=rate(len(ids), total))
                for reason, ids in reasons.items()
            ),
            key=lambda r: (-r.claims, r.reason),
        ),
    )


# --- Overrides ---------------------------------------------------------------------------


async def _overrides(
    session: AsyncSession,
    tasks: list[TaskRow],
    start: datetime,
    end: datetime,
    *,
    per_user: bool,
) -> list[OverrideRate]:
    def unit(claim_id: UUID, stage: str, user: UUID | None) -> tuple[Any, ...]:
        return (claim_id, stage, user) if per_user else (claim_id, stage)

    def group(u: tuple[Any, ...]) -> str:
        return str(u[2]) if per_user else str(u[1])

    decided = {
        unit(t.claim_id, _stage_of(t), t.assignee)
        for t in tasks
        if t.kind in OVERRIDABLE_KINDS and (t.assignee is not None or not per_user)
    }
    entries = list(
        await session.scalars(
            select(OverrideRow).where(OverrideRow.created_at >= start, OverrideRow.created_at < end)
        )
    )
    entry_units = Counter(
        unit(e.claim_id, e.stage, e.user_id)
        for e in entries
        if e.user_id is not None or not per_user
    )
    by_group: dict[str, dict[str, int]] = defaultdict(Counter)
    for u in decided:
        by_group[group(u)]["decided"] += 1
        by_group[group(u)]["overridden"] += u in entry_units
    for u, count in entry_units.items():
        by_group[group(u)]["entries"] += count
    names: dict[str, str] = {}
    if per_user and by_group:
        names = {
            str(u.id): u.name
            for u in await session.scalars(
                select(UserRow).where(UserRow.id.in_({UUID(k) for k in by_group}))
            )
        }
    found = [
        OverrideRate(
            key=key,
            name=names.get(key) if per_user else None,
            decided=c["decided"],
            overridden=c["overridden"],
            rate=rate(c["overridden"], c["decided"]),
            entries=c["entries"],
        )
        for key, c in by_group.items()
    ]
    return sorted(found, key=lambda o: (-o.decided, o.key))


# --- Outcomes ----------------------------------------------------------------------------


def _latest(events: list[EventRow], key: str) -> dict[UUID, Any]:
    """The last value of detail[key] per claim; events are oldest first."""
    found: dict[UUID, Any] = {}
    for event in events:
        value = (event.detail or {}).get(key)
        if value is not None:
            found[event.claim_id] = value
    return found


async def _outcomes(session: AsyncSession, start: datetime, end: datetime) -> Outcomes:
    in_period = (EventRow.created_at >= start, EventRow.created_at < end)
    decisions = _latest(
        await _events(
            session,
            EventRow.stage == Stage.DECISION.value,
            EventRow.action == "stage_completed",
            *in_period,
        ),
        "final_decision",
    )
    risk = _latest(
        await _events(
            session,
            EventRow.stage == Stage.RISK.value,
            EventRow.action == "stage_completed",
            *in_period,
        ),
        "refer",
    )
    crc = await session.scalar(
        select(func.count(func.distinct(TaskRow.claim_id))).where(
            and_(TaskRow.kind == "crc", TaskRow.created_at >= start, TaskRow.created_at < end)
        )
    )
    reconsiderations = await session.scalar(
        select(func.count())
        .select_from(ReconsiderationRow)
        .where(ReconsiderationRow.requested_at >= start, ReconsiderationRow.requested_at < end)
    )
    referred = sum(1 for refer in risk.values() if refer is True)
    return Outcomes(
        decided=len(decisions),
        by_decision=dict(Counter(str(d) for d in decisions.values())),
        referred_to_crc=crc or 0,
        risk_scored=len(risk),
        risk_referred=referred,
        risk_referral_rate=rate(referred, len(risk)),
        reconsiderations=reconsiderations or 0,
    )


# --- Model calls -------------------------------------------------------------------------


def _money(value: Decimal) -> float:
    return round(float(value), 6)


async def _models(session: AsyncSession, start: datetime, end: datetime) -> ModelCosts:
    calls = list(
        await session.scalars(
            select(ModelCallRow).where(
                ModelCallRow.created_at >= start, ModelCallRow.created_at < end
            )
        )
    )
    cost_by_claim: dict[UUID, Decimal] = defaultdict(Decimal)
    latency_by_claim: dict[UUID, int] = defaultdict(int)
    by_role: dict[str, list[ModelCallRow]] = defaultdict(list)
    for call in calls:
        by_role[call.role].append(call)
        if call.claim_id is not None:
            cost_by_claim[call.claim_id] += call.cost or Decimal(0)
            latency_by_claim[call.claim_id] += call.latency_ms
    per_claim = [float(c) for c in cost_by_claim.values()]
    latencies = [float(ms) for ms in latency_by_claim.values()]
    return ModelCosts(
        calls=len(calls),
        claims=len(cost_by_claim),
        total_cost=_money(sum((c.cost or Decimal(0) for c in calls), Decimal(0))),
        cost_per_claim_mean=_money(sum(cost_by_claim.values(), Decimal(0)) / len(per_claim))
        if per_claim
        else None,
        cost_per_claim_p50=percentile(per_claim, 0.5),
        cost_per_claim_p95=percentile(per_claim, 0.95),
        latency_per_claim_p50_ms=percentile(latencies, 0.5),
        latency_per_claim_p95_ms=percentile(latencies, 0.95),
        by_role=sorted(
            (_model_use(role, group) for role, group in by_role.items()),
            key=lambda m: (-m.total_cost, m.role),
        ),
    )


def _model_use(role: str, calls: list[ModelCallRow]) -> ModelUse:
    on_claims = [c for c in calls if c.claim_id is not None]
    claims = {c.claim_id for c in on_claims}
    claim_cost = sum((c.cost or Decimal(0) for c in on_claims), Decimal(0))
    latencies = [float(c.latency_ms) for c in calls]
    return ModelUse(
        role=role,
        calls=len(calls),
        claims=len(claims),
        invalid=sum(1 for c in calls if not c.valid),
        total_cost=_money(sum((c.cost or Decimal(0) for c in calls), Decimal(0))),
        cost_per_claim=_money(claim_cost / len(claims)) if claims else None,
        latency_p50_ms=percentile(latencies, 0.5),
        latency_p95_ms=percentile(latencies, 0.95),
    )
