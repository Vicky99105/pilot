"""Decision through its contract: authority routing by amount, the approver's action, and the
CRC review of partials and rejections."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.seed import seed_claims_db
from app.services.decision.contract import (
    ApproverOutcome,
    CrcOutcome,
    DecisionService,
    InvalidDecision,
    NotAuthorised,
)
from app.services.decision.service import DefaultDecisionService
from app.services.overrides.service import DefaultOverrideLog
from app.shared.seed_ids import DOCTOR_USER_ID, OPS_USER_ID, SENIOR_USER_ID
from app.shared.types import Role, Stage
from tests.services.decided_claims import BREACH_PAYABLE, CLEAN_PAYABLE, adjudicated_claim

D = Decimal


@pytest.fixture
async def service(sessions: async_sessionmaker[AsyncSession]) -> DecisionService:
    await seed_claims_db(sessions)
    return DefaultDecisionService(sessions, Settings().config_dir / "authority_matrix.yaml")


def approve(by: UUID = DOCTOR_USER_ID, task: UUID | None = None) -> ApproverOutcome:
    return ApproverOutcome(action="approve", approver_id=by, task_id=task)


async def test_up_to_rs_1_lakh_routes_to_the_approver_band(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "partial")

    routing = await service.route(claim.claim_id)

    assert (routing.band, routing.role, routing.amount) == (
        "approver",
        Role.APPROVER,
        BREACH_PAYABLE,
    )
    assert routing.authority_amount == BREACH_PAYABLE
    assert routing.draft_decision == "partial" and routing.crc_required is True


async def test_above_rs_1_lakh_routes_to_the_senior_band_and_the_doctor_may_not_approve(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "worked")  # payable Rs 1,33,000

    routing = await service.route(claim.claim_id)
    assert (routing.band, routing.amount) == ("senior_approver", D("133000.00"))

    with pytest.raises(NotAuthorised, match="limit"):
        await service.record(claim.claim_id, approve(DOCTOR_USER_ID))
    record = await service.record(claim.claim_id, approve(SENIOR_USER_ID))
    assert (record.final_decision, record.final_amount) == ("partial", D("133000.00"))
    assert record.approver_id == SENIOR_USER_ID


async def test_a_user_without_the_approver_role_is_refused(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")

    with pytest.raises(NotAuthorised, match="approver"):
        await service.record(claim.claim_id, approve(OPS_USER_ID))


async def test_crc_is_required_for_partial_and_reject_but_not_approve(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    approved = await adjudicated_claim(sessions, "approve")
    partial = await adjudicated_claim(sessions, "partial")
    rejected = await adjudicated_claim(sessions, "reject")

    a = await service.record(approved.claim_id, approve())
    p = await service.record(partial.claim_id, approve())
    r = await service.record(rejected.claim_id, approve())

    assert (a.final_decision, a.final_amount, a.crc_required) == ("approve", CLEAN_PAYABLE, False)
    assert (p.final_decision, p.final_amount, p.crc_required) == ("partial", BREACH_PAYABLE, True)
    assert (r.final_decision, r.final_amount, r.crc_required) == ("reject", D("0.00"), True)


async def test_an_override_records_before_and_after_and_can_make_crc_required(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")

    record = await service.record(
        claim.claim_id,
        ApproverOutcome(
            action="override",
            approver_id=DOCTOR_USER_ID,
            decision="partial",
            amount=D("15000"),
            reason="Pharmacy bill not itemised",
        ),
    )

    assert (record.final_decision, record.final_amount) == ("partial", D("15000.00"))
    assert [(o.field, o.before, o.after) for o in record.overrides] == [
        ("decision", "approve", "partial"),
        ("amount", "19237.50", "15000.00"),
    ]
    assert record.crc_required is True


async def test_an_override_cannot_pay_more_than_the_sheet_or_skip_the_reason(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")

    with pytest.raises(InvalidDecision, match="more than the sheet"):
        await service.record(
            claim.claim_id,
            ApproverOutcome(
                action="override", approver_id=DOCTOR_USER_ID, amount=D("50000"), reason="x"
            ),
        )
    with pytest.raises(InvalidDecision, match="reason"):
        await service.record(
            claim.claim_id,
            ApproverOutcome(action="override", approver_id=DOCTOR_USER_ID, decision="reject"),
        )


async def test_a_send_back_names_an_earlier_stage_and_a_note(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "partial")

    with pytest.raises(InvalidDecision, match="earlier stage"):
        await service.record(
            claim.claim_id,
            ApproverOutcome(
                action="send_back", approver_id=DOCTOR_USER_ID, stage=Stage.SETTLEMENT, note="x"
            ),
        )
    record = await service.record(
        claim.claim_id,
        ApproverOutcome(
            action="send_back",
            approver_id=DOCTOR_USER_ID,
            stage=Stage.BILL_REVIEW,
            note="Recheck the room category",
        ),
    )

    assert (record.action, record.final_decision) == ("send_back", None)
    assert (record.send_back_stage, record.send_back_note) == (
        Stage.BILL_REVIEW,
        "Recheck the room category",
    )
    assert record.crc_required is False


async def test_the_crc_upholds_or_sends_back_and_only_a_crc_reviewer_may(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "partial")
    await service.record(claim.claim_id, approve())

    with pytest.raises(NotAuthorised, match="crc_reviewer"):
        await service.review_crc(
            claim.claim_id, CrcOutcome(action="uphold", reviewer_id=SENIOR_USER_ID)
        )
    sent_back = await service.review_crc(
        claim.claim_id,
        CrcOutcome(
            action="send_back",
            reviewer_id=DOCTOR_USER_ID,
            stage=Stage.ADJUDICATION,
            note="Cite the room category clause",
        ),
    )
    assert sent_back.crc is not None
    assert (sent_back.crc.action, sent_back.crc.send_back_stage) == (
        "send_back",
        Stage.ADJUDICATION,
    )

    await service.record(claim.claim_id, approve())
    upheld = await service.review_crc(
        claim.claim_id, CrcOutcome(action="uphold", reviewer_id=DOCTOR_USER_ID)
    )
    assert upheld.crc is not None and upheld.crc.action == "uphold"
    assert upheld.final_decision == "partial"


async def test_a_crc_review_of_an_approval_is_refused(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")
    await service.record(claim.claim_id, approve())

    with pytest.raises(InvalidDecision, match="no CRC review"):
        await service.review_crc(
            claim.claim_id, CrcOutcome(action="uphold", reviewer_id=DOCTOR_USER_ID)
        )


async def test_recording_is_idempotent_per_task(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "partial")
    task = uuid4()

    first = await service.record(claim.claim_id, approve(task=task))
    again = await service.record(claim.claim_id, approve(task=task))

    assert again == first
    assert await service.latest(claim.claim_id) == first


async def test_a_decision_override_is_written_to_the_override_log(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")

    await service.record(
        claim.claim_id,
        ApproverOutcome(
            action="override",
            approver_id=DOCTOR_USER_ID,
            decision="partial",
            amount=D("15000"),
            reason="Pharmacy bill not itemised",
        ),
    )

    log = await DefaultOverrideLog(sessions).list(claim_id=claim.claim_id)
    assert sorted((o.kind, o.field, o.before, o.after) for o in log) == [
        ("decision_override", "amount", "19237.50", "15000.00"),
        ("decision_override", "decision", "approve", "partial"),
    ]
    assert {o.user_id for o in log} == {DOCTOR_USER_ID}
    assert {o.reason for o in log} == {"Pharmacy bill not itemised"}


async def test_an_approval_as_drafted_writes_no_override(
    sessions: async_sessionmaker[AsyncSession], service: DecisionService
) -> None:
    claim = await adjudicated_claim(sessions, "approve")

    await service.record(
        claim.claim_id, ApproverOutcome(action="approve", approver_id=DOCTOR_USER_ID)
    )

    assert await DefaultOverrideLog(sessions).list(claim_id=claim.claim_id) == []
