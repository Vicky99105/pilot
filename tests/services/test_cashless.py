"""The cashless service through its contract: requests, authorisations against the block,
cancellation and the originals courier deadline."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.integrations.core.contract import BlockRef
from app.services.cashless.contract import (
    AWAITING_DISCHARGE,
    CashlessService,
    NoOriginalsDue,
    NotReadyToAuthorise,
)
from app.services.cashless.service import DefaultCashlessService
from app.shared.seed_ids import DOCTOR_USER_ID, HOSPITAL_USER_ID, OPS_USER_ID
from app.shared.types import RequestType, Stage
from tests.fakes.core import FakeMoneyService

D = Decimal
NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
DISCHARGED = date(2026, 11, 22)


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.money = FakeMoneyService()
        self.records = StageRecordRepository(sessions)
        self.service: CashlessService = DefaultCashlessService(
            sessions, self.money, now=lambda: NOW
        )

    async def claim(self) -> tuple[UUID, UUID]:
        claim_id, submission_id = uuid4(), uuid4()
        async with self.sessions.begin() as session:
            session.add(
                SubmissionRow(id=submission_id, request_type="preauth", channel="hospital",
                              submitted_by=HOSPITAL_USER_ID, form={},
                              clock_deadline=NOW + timedelta(hours=1), created_at=NOW)
            )  # fmt: skip
            await session.flush()
            session.add(
                ClaimRow(id=claim_id, submission_id=submission_id, request_type="preauth",
                         status="intake", status_changed_at=NOW,
                         deadline=NOW + timedelta(hours=1), created_at=NOW)
            )  # fmt: skip
            await session.flush()
            row = await session.get(SubmissionRow, submission_id)
            assert row is not None
            row.claim_id = claim_id
        await self.records.append(
            claim_id,
            Stage.REGISTRATION,
            {"block_ref": BlockRef(block_id="blk-1", amount=D("30000.00"),
                                   available_after=D("0")).model_dump(mode="json")},
            created_by="test",
        )  # fmt: skip
        await self.records.append(
            claim_id,
            Stage.MEDICAL_CODING,
            {"verified_codes": [{"code": {"system": "ICD-10-CM", "version": "FY2027",
                                          "code": "K35.80"}}]},
            created_by="test",
        )  # fmt: skip
        await self.records.append(
            claim_id,
            Stage.DIGITISATION,
            {"admission": {"discharge_date": {"value": DISCHARGED.isoformat(),
                                              "confidence": 1.0}}},
            created_by="test",
        )  # fmt: skip
        return claim_id, submission_id

    async def followup(self, claim_id: UUID, request_type: RequestType, hours: int) -> UUID:
        submission_id = uuid4()
        async with self.sessions.begin() as session:
            session.add(
                SubmissionRow(id=submission_id, request_type=request_type.value,
                              channel="hospital", submitted_by=HOSPITAL_USER_ID, form={},
                              parent_claim_id=claim_id, claim_id=claim_id,
                              clock_deadline=NOW + timedelta(hours=hours), created_at=NOW)
            )  # fmt: skip
        return submission_id

    async def decide(self, claim_id: UUID, decision: str, amount: str, crc: bool = False) -> None:
        """The decision, on a sheet whose payable is the amount after the 5% co-pay."""
        admissible = (D(amount) / D("0.95")).quantize(D("0.01"))
        sheet = {"admissible_total": str(admissible), "payable": amount}
        await self.records.append(claim_id, Stage.BILL_REVIEW, sheet, created_by="test")
        payload: dict[str, Any] = {
            "final_decision": decision,
            "final_amount": amount,
            "crc_required": crc,
            "crc": None,
        }
        await self.records.append(claim_id, Stage.DECISION, payload, created_by="test")

    async def status(self, claim_id: UUID) -> ClaimRow:
        async with self.sessions() as session:
            row = await session.get(ClaimRow, claim_id)
        assert row is not None
        return row


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


async def test_begin_request_makes_the_submission_current_and_moves_the_deadline(
    env: Env,
) -> None:
    claim_id, _ = await env.claim()
    enhancement = await env.followup(claim_id, RequestType.ENHANCEMENT, hours=1)

    request = await env.service.begin_request(claim_id, enhancement)

    claim = await env.status(claim_id)
    assert request.request_type is RequestType.ENHANCEMENT
    assert request.clock_deadline == NOW + timedelta(hours=1)
    assert (claim.current_request, claim.current_submission_id) == ("enhancement", enhancement)
    assert claim.deadline == request.clock_deadline


async def test_a_preauth_sets_the_block_to_the_approved_amount(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "partial", "26500.00")

    first = await env.service.authorise(claim_id)
    again = await env.service.authorise(claim_id)

    assert again == first
    assert (first.previous_total, first.approved_now, first.approved_total) == (
        D("0"), D("26500.00"), D("26500.00"))  # fmt: skip
    assert first.diagnosis_codes == ["K35.80"]
    assert first.approved_admissible == D("27894.74")  # before co-pay: what R26 compares
    assert (first.block_id, first.blocked, first.status) == (
        "blk-1", D("26500.00"), "awaiting_discharge")  # fmt: skip
    [adjusted] = env.money.adjustments.values()
    assert adjusted.amount == D("26500.00")


async def test_an_enhancement_raises_the_block_by_the_approved_delta(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "approve", "26500.00")
    await env.service.authorise(claim_id)
    enhancement = await env.followup(claim_id, RequestType.ENHANCEMENT, hours=1)
    await env.service.begin_request(claim_id, enhancement)
    await env.decide(claim_id, "partial", "33000.00")

    raised = await env.service.authorise(claim_id)

    assert raised.request_type is RequestType.ENHANCEMENT
    assert (raised.previous_total, raised.approved_now, raised.approved_total) == (
        D("26500.00"), D("6500.00"), D("33000.00"))  # fmt: skip
    assert raised.blocked == D("33000.00")
    assert raised.approved_admissible == D("34736.84")
    assert raised.diagnosis_codes == ["K35.80"]  # kept from the pre-auth
    assert [a.amount for a in env.money.adjustments.values()] == [D("26500.00"), D("33000.00")]


async def test_a_rejected_enhancement_keeps_the_approved_total(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "approve", "26500.00")
    await env.service.authorise(claim_id)
    await env.service.begin_request(
        claim_id, await env.followup(claim_id, RequestType.ENHANCEMENT, hours=1)
    )
    await env.decide(claim_id, "reject", "0.00", crc=False)

    kept = await env.service.authorise(claim_id)

    assert (kept.approved_now, kept.approved_total, kept.blocked) == (
        D("0.00"), D("26500.00"), D("26500.00"))  # fmt: skip


async def test_a_rejected_preauth_releases_the_block_and_closes_the_claim(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "reject", "0.00")

    rejected = await env.service.authorise(claim_id)

    assert (rejected.status, rejected.approved_total, rejected.blocked) == (
        "rejected", D("0.00"), D("0.00"))  # fmt: skip
    assert list(env.money.releases.values()) == [("blk-1", D("0.00"))]
    assert (await env.status(claim_id)).status == "closed"


async def test_the_discharge_authorisation_starts_the_originals_deadline(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "approve", "33000.00")
    await env.service.authorise(claim_id)
    await env.service.begin_request(
        claim_id, await env.followup(claim_id, RequestType.DISCHARGE, hours=3)
    )
    await env.decide(claim_id, "partial", "32750.00")

    final = await env.service.authorise(claim_id)

    assert final.status == "final"
    assert (final.approved_total, final.blocked) == (D("32750.00"), D("33000.00"))
    due = datetime.combine(DISCHARGED + timedelta(days=15), datetime.min.time(), UTC)
    assert final.originals_due_at == due
    assert (await env.status(claim_id)).originals_due_at == due
    assert len(env.money.adjustments) == 1  # settlement releases the block, not this

    received = await env.service.mark_originals_received(claim_id, OPS_USER_ID)
    assert (received.received_by, received.received_at) == (OPS_USER_ID, NOW)


async def test_originals_are_due_only_after_the_discharge(env: Env) -> None:
    claim_id, _ = await env.claim()
    with pytest.raises(NoOriginalsDue):
        await env.service.mark_originals_received(claim_id, DOCTOR_USER_ID)


async def test_a_decision_the_crc_has_not_upheld_is_not_authorised(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "partial", "26500.00", crc=True)

    with pytest.raises(NotReadyToAuthorise):
        await env.service.authorise(claim_id)


async def test_awaiting_discharge_is_the_status_between_requests(env: Env) -> None:
    claim_id, _ = await env.claim()

    await env.service.mark_awaiting_discharge(claim_id)

    assert (await env.status(claim_id)).status == AWAITING_DISCHARGE


async def test_a_cancellation_releases_the_block_and_closes_the_claim(env: Env) -> None:
    claim_id, submission_id = await env.claim()
    await env.service.begin_request(claim_id, submission_id)
    await env.decide(claim_id, "approve", "26500.00")
    await env.service.authorise(claim_id)
    cancellation = await env.followup(claim_id, RequestType.CANCELLATION, hours=0)

    cancelled = await env.service.cancel(claim_id, cancellation)
    again = await env.service.cancel(claim_id, cancellation)

    assert again == cancelled
    assert (cancelled.status, cancelled.approved_total, cancelled.blocked) == (
        "cancelled", D("0.00"), D("0.00"))  # fmt: skip
    assert list(env.money.releases.values()) == [("blk-1", D("0.00"))]
    claim = await env.status(claim_id)
    assert claim.status == "closed" and claim.closed_at == NOW
