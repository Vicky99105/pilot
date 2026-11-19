"""Settlement through its contract: payee checks, then the finance release, then the payment
instruction, then the block release and closure. Order matters."""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow
from app.database.seed import seed_claims_db
from app.services.settlement.contract import (
    PayeeCheckFailed,
    PaymentNotReleased,
    SettlementService,
)
from app.services.settlement.service import DefaultSettlementService
from app.shared.seed_ids import FINANCE_USER_ID, OPS_USER_ID
from tests.fakes.core import FakeMoneyService
from tests.services.decided_claims import BREACH_PAYABLE, bank, decided_claim

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
