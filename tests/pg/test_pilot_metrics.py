"""P3-4 pilot metrics on real Postgres, after migrations: the period filters on timestamptz,
JSONB task payloads, event details and query items, and the count queries."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ModelCallRow, OverrideRow, QueryRow
from app.shared.types import Role
from tests.database.factories import add_user
from tests.services.test_monitoring_metrics import AFTER, BEFORE, END, START, Data, at

pytestmark = pytest.mark.pg


async def test_pilot_metrics_on_postgres(pg_sessions: async_sessionmaker[AsyncSession]) -> None:
    data = Data(pg_sessions)
    ops = await add_user(pg_sessions, "Ops", [Role.CLAIMS_OPERATOR])
    claim_id = await data.claim(deadline=at(5))
    await data.claim(created_at=BEFORE)
    for hours in (1, 3):
        await data.task(claim_id, kind="fix_fields", role=Role.CLAIMS_OPERATOR,
                        stage="digitisation", opened=at(0), completed=at(hours),
                        assignee=ops)  # fmt: skip
    await data.add(
        OverrideRow(id=uuid4(), claim_id=claim_id, stage="digitisation", kind="field_fix",
                    field="identity.patient_name", before={"v": "a"}, after="b", reason="",
                    actor=f"user:{ops}", user_id=ops, created_at=at(1)),
        QueryRow(id=uuid4(), claim_id=claim_id, status="open", deadline=AFTER, raised_at=at(4),
                 items=[{"text": "bill", "source": "missing_document", "doc_type": "final_bill"}]),
        ModelCallRow(id=uuid4(), claim_id=claim_id, purpose="p", role="classifier",
                     provider="x", model="m", attempt=1, prompt=[], output="", valid=True,
                     cost=Decimal("0.012"), latency_ms=800, created_at=at(1)),
    )  # fmt: skip
    await data.event(claim_id, "intake", "stage_completed", at(0))
    await data.event(claim_id, "risk", "stage_completed", at(2), refer=True)
    await data.event(claim_id, "decision", "stage_completed", at(6), final_decision="partial")
    await data.event(claim_id, "settlement", "claim_closed", at(8))
    cashless = await data.claim("preauth")
    await data.event(cashless, "intake", "request_started", at(0), request_type="preauth")
    await data.event(cashless, "monitoring", "clock_breached", at(1), request_type="preauth")
    await data.event(cashless, "communication", "stage_completed", at(2))

    metrics = await data.monitoring.metrics(start=START, end=END)

    [queue] = metrics.turnaround_by_role
    assert (queue.key, queue.completed, queue.p50_seconds, queue.p95_seconds) == (
        "claims_operator",
        2,
        7200,
        10440,
    )
    assert [t.key for t in metrics.turnaround_by_stage] == ["digitisation"]
    rows = {r.request_type: r for r in metrics.request_turnaround}
    assert (rows["reimbursement"].p50_seconds, rows["reimbursement"].breaches) == (8 * 3600, 1)
    assert (rows["preauth"].p50_seconds, rows["preauth"].breaches) == (2 * 3600, 1)
    assert (metrics.queries.claims_received, metrics.queries.claims_queried) == (2, 1)
    assert [r.reason for r in metrics.queries.by_reason] == ["final_bill"]
    [stage] = metrics.overrides_by_stage
    assert (stage.key, stage.decided, stage.overridden, stage.entries) == ("digitisation", 1, 1, 1)
    [user] = metrics.overrides_by_user
    assert (user.name, user.rate) == ("Ops", 1.0)
    outcomes = metrics.outcomes
    assert (outcomes.by_decision, outcomes.risk_referral_rate) == ({"partial": 1}, 1.0)
    assert (metrics.models.total_cost, metrics.models.latency_per_claim_p50_ms) == (0.012, 800)
    assert END - START == timedelta(days=7)
