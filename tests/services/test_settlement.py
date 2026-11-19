"""Settlement through its contract: payee checks, then the finance release, then the payment
instruction, then the block release and closure. Order matters."""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.settlement.contract import (
    PayeeCheckFailed,
    PaymentNotReleased,
    SettlementService,
)
from app.services.settlement.service import DefaultSettlementService
from app.shared.seed_ids import FINANCE_USER_ID, OPS_USER_ID
from app.shared.types import Stage
from tests.fakes.core import FakeMoneyService
from tests.services.decided_claims import BREACH_PAYABLE, bank, decided_claim

SUNRISE_PAYEE = "Sunrise Multispeciality Hospital"

D = Decimal


def service(
    sessions: async_sessionmaker[AsyncSession], money: FakeMoneyService
) -> SettlementService:
    return DefaultSettlementService(sessions, money)


async def _claim_row(sessions: async_sessionmaker[AsyncSession], claim_id: object) -> ClaimRow:
    async with sessions() as session:
        row = await session.get(ClaimRow, claim_id)
    assert row is not None
    return row


@pytest.fixture(autouse=True)
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_claims_db(sessions)


async def test_payee_checks_pass_and_nothing_is_paid_before_the_release(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "partial")
    settlement = service(sessions, claim.money)

    checked = await settlement.check_payee(claim.claim_id)

    assert checked.status == "awaiting_release"
    assert [(c.check_id, c.passed) for c in checked.payee_checks] == [
        ("ifsc_format", True),
        ("holder_matches_member", True),
        ("cheque_matches_claim_form", True),
    ]
    assert checked.payee is not None and checked.payee.account_number == "123456789012"
    assert checked.paid_amount == BREACH_PAYABLE
    with pytest.raises(PaymentNotReleased):
        await settlement.settle(claim.claim_id, released_by=None)
    with pytest.raises(PaymentNotReleased, match="finance"):
        await settlement.settle(claim.claim_id, released_by=OPS_USER_ID)
    assert claim.money.payments == {} and claim.money.releases == {}
    assert (await _claim_row(sessions, claim.claim_id)).closed_at is None


async def test_after_the_release_one_payment_then_the_block_release_then_closure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "partial")
    settlement = service(sessions, claim.money)
    await settlement.check_payee(claim.claim_id)

    settled = await settlement.settle(claim.claim_id, released_by=FINANCE_USER_ID)
    again = await settlement.settle(claim.claim_id, released_by=FINANCE_USER_ID)

    assert again == settled
    [(key, payment)] = claim.money.payments.items()
    assert key == f"{claim.claim_id}pay{settled.decision_version}"
    assert payment.amount == BREACH_PAYABLE and settled.payment_ref == payment
    [(block_id, utilised)] = claim.money.releases.values()
    [block] = claim.money.blocks.values()
    assert (block_id, utilised) == (block.block_id, BREACH_PAYABLE)
    assert (settled.utilised, settled.released) == (BREACH_PAYABLE, block.amount - BREACH_PAYABLE)
    row = await _claim_row(sessions, claim.claim_id)
    assert (row.status, row.closed_at) == ("closed", settled.closed_at)
    assert settled.status == "settled" and settled.released_by == FINANCE_USER_ID


async def test_a_rejected_claim_releases_the_whole_block_and_closes_without_payment(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "reject")
    settlement = service(sessions, claim.money)

    checked = await settlement.check_payee(claim.claim_id)
    settled = await settlement.settle(claim.claim_id, released_by=None)

    assert (checked.payee, checked.payee_checks) == (None, [])
    assert claim.money.payments == {}
    [(_, utilised)] = claim.money.releases.values()
    [block] = claim.money.blocks.values()
    assert utilised == D("0.00") and settled.released == block.amount
    assert (await _claim_row(sessions, claim.claim_id)).status == "closed"


async def test_failed_payee_checks_block_the_payment(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cheque = bank(holder="RAVI KUMAR", account="999988887777", ifsc="SBIN001234")
    claim = await decided_claim(sessions, "partial", cheque=cheque)
    settlement = service(sessions, claim.money)

    checked = await settlement.check_payee(claim.claim_id)

    assert [(c.check_id, c.passed) for c in checked.payee_checks] == [
        ("ifsc_format", False),
        ("holder_matches_member", False),
        ("cheque_matches_claim_form", False),
    ]
    with pytest.raises(PayeeCheckFailed, match="ifsc_format"):
        await settlement.settle(claim.claim_id, released_by=FINANCE_USER_ID)
    assert claim.money.payments == {}


# --- P2-1: the Submit-area form's bank details are a third source --------------------------


async def _with_form_bank(
    sessions: async_sessionmaker[AsyncSession], claim_id: UUID, account: str, ifsc: str
) -> None:
    records = StageRecordRepository(sessions)
    intake = await records.latest(claim_id, Stage.INTAKE)
    assert intake is not None
    form = {**intake.payload["form"], "bank_account_number": account, "ifsc": ifsc}
    await records.append(claim_id, Stage.INTAKE, {**intake.payload, "form": form}, created_by="t")


async def test_the_submit_form_bank_details_agreeing_with_the_cheque_pass(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "partial")
    await _with_form_bank(sessions, claim.claim_id, "123456789012", "SBIN0001234")

    checked = await service(sessions, claim.money).check_payee(claim.claim_id)

    assert ("cheque_matches_submit_form", True) in [
        (c.check_id, c.passed) for c in checked.payee_checks
    ]


async def test_submit_form_bank_details_that_differ_from_the_cheque_block_the_payment(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "partial")
    await _with_form_bank(sessions, claim.claim_id, "111122223333", "SBIN0001234")
    settlement = service(sessions, claim.money)

    checked = await settlement.check_payee(claim.claim_id)

    [check] = [c for c in checked.payee_checks if c.check_id == "cheque_matches_submit_form"]
    assert check.passed is False
    assert "111122223333" in check.detail and "123456789012" in check.detail
    with pytest.raises(PayeeCheckFailed, match="cheque_matches_submit_form"):
        await settlement.settle(claim.claim_id, released_by=FINANCE_USER_ID)
    assert claim.money.payments == {}


async def test_a_form_without_bank_details_adds_no_third_check(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    claim = await decided_claim(sessions, "partial")

    checked = await service(sessions, claim.money).check_payee(claim.claim_id)

    assert "cheque_matches_submit_form" not in [c.check_id for c in checked.payee_checks]


# --- P2-5: a cashless claim pays the hospital ------------------------------------------------


async def test_a_cashless_discharge_pays_the_hospital_and_releases_the_authorised_block(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The payee is the account in the hospital's agreement, not the member's cheque. The
    block is the authorised one (the enhancement raised it), so the release returns the
    authorised block less the amount paid."""
    claim = await decided_claim(sessions, "partial")
    await seed_claims_db(sessions)  # the hospital agreements, with their payee accounts
    async with sessions.begin() as session:
        row = await session.get(ClaimRow, claim.claim_id)
        assert row is not None
        row.request_type, row.current_request = "preauth", "discharge"
    [block] = claim.money.blocks.values()
    await StageRecordRepository(sessions).append(
        claim.claim_id,
        Stage.AUTHORISATION,
        {"block_id": block.block_id, "blocked": "45000.00", "status": "final"},
        created_by="test",
    )
    settlement = service(sessions, claim.money)

    checked = await settlement.check_payee(claim.claim_id)
    settled = await settlement.settle(claim.claim_id, released_by=FINANCE_USER_ID)

    assert checked.payee is not None
    assert (checked.payee.kind, checked.payee.name) == ("hospital", SUNRISE_PAYEE)
    assert [(c.check_id, c.passed) for c in checked.payee_checks] == [
        ("ifsc_format", True),
        ("agreement_payee_on_file", True),
    ]
    [payment] = claim.money.payments.values()
    assert payment.amount == BREACH_PAYABLE
    assert list(claim.money.releases.values()) == [(block.block_id, BREACH_PAYABLE)]
    assert settled.released == D("45000.00") - BREACH_PAYABLE
