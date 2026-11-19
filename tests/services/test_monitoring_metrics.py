"""Pilot metrics (P3-4) through the Monitoring contract, on SQLite. Rows are written as the
workflow and services write them: tasks, events, queries, overrides, reconsiderations and
model calls. Every metric counts what happened inside the period asked for."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    ClaimRow,
    EventRow,
    ModelCallRow,
    OverrideRow,
    QueryRow,
    ReconsiderationRow,
    SubmissionRow,
    TaskRow,
)
from app.services.monitoring.contract import Monitoring
from app.services.monitoring.service import DefaultMonitoring
from app.shared.types import Role
from tests.database.factories import add_user

START = datetime(2026, 11, 10, tzinfo=UTC)
END = START + timedelta(days=7)
BEFORE = START - timedelta(days=2)
AFTER = END + timedelta(days=1)


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


class Data:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.monitoring: Monitoring = DefaultMonitoring(sessions, now=lambda: END)

    async def add(self, *rows: Any) -> None:
        async with self.sessions.begin() as session:
            session.add_all(rows)

    async def claim(
        self,
        request_type: str = "reimbursement",
        created_at: datetime = START,
        deadline: datetime | None = None,
    ) -> UUID:
        claim_id = uuid4()
        await self.add(
            ClaimRow(id=claim_id, request_type=request_type, status="intake",
                     status_changed_at=created_at, created_at=created_at, deadline=deadline)
        )  # fmt: skip
        return claim_id

    async def task(
        self,
        claim_id: UUID,
        *,
        kind: str,
        role: Role,
        stage: str,
        opened: datetime,
        completed: datetime | None,
        assignee: UUID | None = None,
        deadline: datetime | None = None,
    ) -> None:
        await self.add(
            TaskRow(id=uuid4(), claim_id=claim_id, workflow_id="wf", kind=kind, role=role.value,
                    title=kind, payload={"stage": stage},
                    deadline=deadline or opened + timedelta(hours=4),
                    status="done" if completed else "open", assignee=assignee,
                    outcome={} if completed else None, created_at=opened,
                    completed_at=completed)
        )  # fmt: skip

    async def event(
        self, claim_id: UUID, stage: str, action: str, when: datetime, **detail: Any
    ) -> None:
        await self.add(
            EventRow(id=uuid4(), claim_id=claim_id, stage=stage, action=action, actor="system",
                     detail=detail or None, created_at=when)
        )  # fmt: skip


async def test_task_turnaround_per_queue_and_stage_within_the_period(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    claim_id = await data.claim()
    for hours in (1, 2, 3, 4):  # opened at START, completed after 1, 2, 3 and 4 hours
        await data.task(claim_id, kind="confirm_registration", role=Role.CLAIMS_OPERATOR,
                        stage="registration", opened=at(0), completed=at(hours),
                        deadline=at(2.5))  # fmt: skip
    await data.task(claim_id, kind="confirm_deductions", role=Role.BILL_PROCESSOR,
                    stage="bill_review", opened=at(0), completed=at(0.5))  # fmt: skip
    # Left out: completed before the period, and not completed yet.
    await data.task(claim_id, kind="confirm_registration", role=Role.CLAIMS_OPERATOR,
                    stage="registration", opened=BEFORE,
                    completed=BEFORE + timedelta(hours=9))  # fmt: skip
    await data.task(claim_id, kind="confirm_registration", role=Role.CLAIMS_OPERATOR,
                    stage="registration", opened=at(1), completed=None)  # fmt: skip

    metrics = await data.monitoring.metrics(start=START, end=END)

    by_role = {t.key: t for t in metrics.turnaround_by_role}
    ops = by_role["claims_operator"]
    assert (ops.completed, ops.p50_seconds, ops.p95_seconds, ops.late) == (4, 9000, 13860, 2)
    assert by_role["bill_processor"].p50_seconds == 1800
    by_stage = {t.key: t.completed for t in metrics.turnaround_by_stage}
    assert by_stage == {"registration": 4, "bill_review": 1}
    assert (metrics.start, metrics.end) == (START, END)


async def test_request_turnaround_by_type_with_clock_breaches(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    # Reimbursement: received (first event) to claim_closed. One closed after its deadline.
    on_time = await data.claim(deadline=at(100))
    await data.event(on_time, "intake", "stage_completed", at(0))
    await data.event(on_time, "settlement", "claim_closed", at(10))
    late = await data.claim(deadline=at(5))
    await data.event(late, "intake", "stage_completed", at(0))
    await data.event(late, "settlement", "claim_closed", at(30))
    # Cashless: each request_started to the first communication after it.
    cashless = await data.claim("preauth")
    await data.event(cashless, "intake", "request_started", at(0), request_type="preauth")
    await data.event(cashless, "communication", "stage_completed", at(0.5))
    await data.event(cashless, "intake", "request_started", at(20), request_type="enhancement")
    await data.event(cashless, "monitoring", "clock_breached", at(21), request_type="enhancement")
    await data.event(cashless, "communication", "stage_completed", at(22))
    await data.event(cashless, "intake", "request_started", at(40), request_type="discharge")
    # The discharge is answered after the period: not counted.
    await data.event(cashless, "communication", "stage_completed", AFTER)
    # A reconsideration: its turnaround runs from the request, not the first event.
    reconsidered = await data.claim(created_at=BEFORE)
    await data.event(reconsidered, "intake", "stage_completed", BEFORE)
    await data.event(reconsidered, "settlement", "claim_closed", BEFORE + timedelta(hours=1))
    await data.event(reconsidered, "intake", "reconsideration_requested", at(50))
    await data.event(reconsidered, "settlement", "claim_closed", at(54))

    metrics = await data.monitoring.metrics(start=START, end=END)

    rows = {r.request_type: r for r in metrics.request_turnaround}
    assert (rows["reimbursement"].answered, rows["reimbursement"].breaches) == (2, 1)
    assert rows["reimbursement"].p50_seconds == 20 * 3600
    assert (rows["preauth"].answered, rows["preauth"].p50_seconds) == (1, 1800)
    assert (rows["enhancement"].answered, rows["enhancement"].breaches) == (1, 1)
    assert rows["enhancement"].p50_seconds == 2 * 3600
    assert (rows["discharge"].answered, rows["discharge"].p50_seconds) == (0, None)
    assert rows["reconsideration"].p50_seconds == 4 * 3600
    assert metrics.clock_breaches == 1  # cashless clocks only; late reimbursements are above


async def test_query_rate_overall_and_by_missing_document(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    claims = [await data.claim() for _ in range(4)]
    await data.claim(created_at=BEFORE)  # received before the period: not in the base
    await data.add(
        QueryRow(id=uuid4(), claim_id=claims[0], status="open", deadline=AFTER, raised_at=at(5),
                 items=[{"text": "bill", "source": "missing_document", "doc_type": "final_bill"},
                        {"text": "notes", "source": "medical", "doc_type": None}]),
        QueryRow(id=uuid4(), claim_id=claims[1], status="answered", deadline=AFTER,
                 raised_at=at(6),
                 items=[{"text": "bill", "source": "missing_document", "doc_type": "final_bill"}]),
    )  # fmt: skip

    queries = (await data.monitoring.metrics(start=START, end=END)).queries

    assert (queries.claims_received, queries.claims_queried, queries.rate) == (4, 2, 0.5)
    assert [(r.reason, r.claims, r.rate) for r in queries.by_reason] == [
        ("final_bill", 2, 0.5),
        ("medical", 1, 0.25),
    ]


async def test_override_rate_per_stage_and_user(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    ops = await add_user(sessions, "Ops", [Role.CLAIMS_OPERATOR, Role.BILL_PROCESSOR])
    first, second = await data.claim(), await data.claim()
    for claim_id in (first, second):
        await data.task(
            claim_id,
            kind="fix_fields",
            role=Role.CLAIMS_OPERATOR,
            stage="digitisation",
            opened=at(0),
            completed=at(1),
            assignee=ops,
        )
        await data.task(claim_id, kind="confirm_deductions", role=Role.BILL_PROCESSOR,
                        stage="bill_review", opened=at(0), completed=at(1),
                        assignee=ops)  # fmt: skip
    # A kind that cannot change a suggestion does not count as decided.
    await data.task(first, kind="investigate", role=Role.INVESTIGATOR, stage="risk",
                    opened=at(0), completed=at(1), assignee=ops)  # fmt: skip
    # Two field fixes on the first claim's digitisation: one decision overridden, two entries.
    for field in ("identity.patient_name", "admission.date"):
        await data.add(
            OverrideRow(id=uuid4(), claim_id=first, stage="digitisation", kind="field_fix",
                        field=field, before="a", after="b", reason="", actor=f"user:{ops}",
                        user_id=ops, created_at=at(1))
        )  # fmt: skip

    metrics = await data.monitoring.metrics(start=START, end=END)

    stages = {o.key: o for o in metrics.overrides_by_stage}
    assert (stages["digitisation"].decided, stages["digitisation"].overridden) == (2, 1)
    assert (stages["digitisation"].rate, stages["digitisation"].entries) == (0.5, 2)
    assert (stages["bill_review"].rate, stages["bill_review"].entries) == (0.0, 0)
    assert "risk" not in stages
    [user] = metrics.overrides_by_user
    assert (user.key, user.name, user.decided, user.overridden, user.rate) == (
        str(ops),
        "Ops",
        4,
        1,
        0.25,
    )


async def test_outcomes_risk_referrals_and_reconsiderations(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    member = await add_user(sessions, "Member", [Role.MEMBER])
    approved, partial, rejected, resent = [await data.claim() for _ in range(4)]
    await data.event(approved, "decision", "stage_completed", at(1), final_decision="approve")
    await data.event(partial, "decision", "stage_completed", at(2), final_decision="partial")
    await data.event(rejected, "decision", "stage_completed", at(3), final_decision="reject")
    # Decided twice in the period (a reconsideration): the latest decision counts.
    await data.event(resent, "decision", "stage_completed", at(1), final_decision="reject")
    await data.event(resent, "decision", "stage_completed", at(9), final_decision="approve")
    # The decision stage_completed without a final decision is not an outcome.
    await data.event(approved, "decision", "stage_completed", at(0.5))
    for claim_id in (partial, rejected):
        await data.task(claim_id, kind="crc", role=Role.CRC_REVIEWER, stage="decision",
                        opened=at(2), completed=at(3))  # fmt: skip
    await data.event(approved, "risk", "stage_completed", at(1), refer=False, score=10)
    await data.event(rejected, "risk", "stage_completed", at(1), refer=True, score=80)
    submission = uuid4()
    await data.add(SubmissionRow(id=submission, request_type="reconsideration", form={}))
    await data.add(
        ReconsiderationRow(id=uuid4(), claim_id=resent, number=1, submission_id=submission,
                           workflow_id="wf-r", grounds="g", requested_by=member,
                           requested_at=at(5), previous_amount=Decimal("0"),
                           previously_paid=Decimal("0"))
    )  # fmt: skip

    outcomes = (await data.monitoring.metrics(start=START, end=END)).outcomes

    assert outcomes.decided == 4
    assert outcomes.by_decision == {"approve": 2, "partial": 1, "reject": 1}
    assert outcomes.referred_to_crc == 2
    assert (outcomes.risk_scored, outcomes.risk_referred, outcomes.risk_referral_rate) == (
        2,
        1,
        0.5,
    )
    assert outcomes.reconsiderations == 1


async def test_model_cost_and_latency_per_claim_and_role(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = Data(sessions)
    first, second = await data.claim(), await data.claim()

    def call(claim_id: UUID | None, role: str, cost: str | None, ms: int, **kw: Any) -> Any:
        return ModelCallRow(
            id=uuid4(), claim_id=claim_id, purpose="p", role=role, provider="x", model="m",
            attempt=1, prompt=[], output="", valid=kw.get("valid", True),
            cost=Decimal(cost) if cost else None, latency_ms=ms,
            created_at=kw.get("when", at(1)),
        )  # fmt: skip

    await data.add(
        call(first, "classifier", "0.010", 1000),
        call(first, "adjudicator", "0.030", 3000),
        call(second, "classifier", "0.020", 2000, valid=False),
        call(second, "classifier", None, 500),
        call(None, "classifier", "0.005", 100),  # no claim: counts for its role only
        call(first, "classifier", "9.000", 9000, when=BEFORE),  # before the period
    )

    models = (await data.monitoring.metrics(start=START, end=END)).models

    assert (models.calls, models.claims, models.total_cost) == (5, 2, 0.065)
    assert (models.cost_per_claim_p50, models.cost_per_claim_mean) == (0.03, 0.03)
    assert models.latency_per_claim_p50_ms == 3250
    roles = {r.role: r for r in models.by_role}
    classifier = roles["classifier"]
    assert (classifier.calls, classifier.claims, classifier.invalid) == (4, 2, 1)
    assert (classifier.total_cost, classifier.cost_per_claim) == (0.035, 0.015)
    assert (classifier.latency_p50_ms, classifier.latency_p95_ms) == (750, 1850)
    assert roles["adjudicator"].cost_per_claim == 0.03


async def test_an_empty_period_has_counts_of_zero_and_no_rates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    metrics = await Data(sessions).monitoring.metrics(start=START, end=END)

    assert metrics.turnaround_by_role == [] and metrics.overrides_by_user == []
    assert (metrics.queries.claims_received, metrics.queries.rate) == (0, None)
    assert (metrics.outcomes.decided, metrics.outcomes.risk_referral_rate) == (0, None)
    assert (metrics.models.calls, metrics.models.cost_per_claim_p50) == (0, None)
    assert {r.request_type for r in metrics.request_turnaround} == {
        "reimbursement",
        "preauth",
        "enhancement",
        "discharge",
        "reconsideration",
    }
