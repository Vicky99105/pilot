"""The Http core clients against the real mock_core router, over an in-process transport."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.integrations.core.contract import (
    BlockNotFound,
    InsufficientSumInsured,
    MoneyService,
    Payee,
    PermissionService,
    PolicyService,
)
from app.integrations.core.http import (
    HttpMoneyService,
    HttpPermissionService,
    HttpPolicyService,
)
from app.mock_core.router import router as mock_core_router
from app.mock_core.seed import seed_mock_core
from app.shared.seed_ids import (
    HOSPITAL_ROHINI_IDS,
    HOSPITAL_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
)

PAYEE = Payee(kind="member", name="Asha Verma", account_number="000111222333", ifsc="MOCK0000001")


@dataclass
class Core:
    policy: PolicyService
    money: MoneyService
    permission: PermissionService


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Core]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mock_core.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await seed_mock_core(engine, sessions)
    app = FastAPI()
    app.state.mock_core_sessions = sessions
    app.include_router(mock_core_router, prefix="/mock-core")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app")
    base = "http://app/mock-core"
    yield Core(
        HttpPolicyService(base, http),
        HttpMoneyService(base, http),
        HttpPermissionService(base, http),
    )
    await http.aclose()
    await engine.dispose()


async def test_find_member_by_member_id_returns_the_policy(core: Core) -> None:
    policy = await core.policy.find_member(policy_number=None, member_id=MEMBER_MEMBER_ID)

    assert policy is not None
    assert policy.policy_number == MEMBER_POLICY_NUMBER
    assert MEMBER_MEMBER_ID in {m.member_id for m in policy.insured_members}
    assert Decimal("300000") <= policy.sum_insured <= Decimal("500000")
    assert policy.premium_payments


async def test_find_member_by_policy_number_and_unknown_member(core: Core) -> None:
    found = await core.policy.find_member(policy_number=MEMBER_POLICY_NUMBER, member_id=None)
    missing = await core.policy.find_member(policy_number=None, member_id="NOPE")

    assert found is not None and found.policy_number == MEMBER_POLICY_NUMBER
    assert missing is None


async def test_seed_has_ten_members_one_recent_inception_and_declared_peds(core: Core) -> None:
    policies = {}
    for n in range(1, 30):
        for m in range(1, 5):
            p = await core.policy.find_member(
                policy_number=None, member_id=f"MOCK-M-{100 + n:06d}-{m:02d}"
            )
            if p:
                policies[f"{n}-{m}"] = p

    assert len(policies) == 10
    assert any((date(2026, 11, 19) - p.inception_date).days < 30 for p in policies.values())
    assert any(p.declared_peds for p in policies.values())
    assert all(c.system == "ICD-10-CM" for p in policies.values() for c in p.declared_peds)


async def test_block_is_idempotent_on_key(core: Core) -> None:
    first = await core.money.block_sum_insured(
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
        amount=Decimal("50000.00"),
        idempotency_key="block-1",
    )
    again = await core.money.block_sum_insured(
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
        amount=Decimal("50000.00"),
        idempotency_key="block-1",
    )
    other = await core.money.block_sum_insured(
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
        amount=Decimal("1000.00"),
        idempotency_key="block-2",
    )

    assert again == first
    assert other.block_id != first.block_id
    assert other.available_after == first.available_after - Decimal("1000.00")


async def test_block_over_available_sum_insured_is_refused(core: Core) -> None:
    with pytest.raises(InsufficientSumInsured):
        await core.money.block_sum_insured(
            policy_number=MEMBER_POLICY_NUMBER,
            member_id=MEMBER_MEMBER_ID,
            amount=Decimal("99999999.00"),
            idempotency_key="too-much",
        )


async def test_release_is_idempotent_and_adds_utilised_once(core: Core) -> None:
    before = await core.policy.find_member(policy_number=MEMBER_POLICY_NUMBER, member_id=None)
    block = await core.money.block_sum_insured(
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
        amount=Decimal("40000.00"),
        idempotency_key="b",
    )

    await core.money.release_block(
        block_id=block.block_id, utilised=Decimal("32500.50"), idempotency_key="r"
    )
    await core.money.release_block(
        block_id=block.block_id, utilised=Decimal("32500.50"), idempotency_key="r"
    )

    after = await core.policy.find_member(policy_number=MEMBER_POLICY_NUMBER, member_id=None)
    assert before is not None and after is not None
    assert after.utilised - before.utilised == Decimal("32500.50")


async def test_release_unknown_block_raises(core: Core) -> None:
    with pytest.raises(BlockNotFound):
        await core.money.release_block(
            block_id=str(uuid4()), utilised=Decimal("0"), idempotency_key="x"
        )


async def test_payment_instruction_is_idempotent_on_key(core: Core) -> None:
    first = await core.money.create_payment_instruction(
        payee=PAYEE, amount=Decimal("1500.00"), claim_number="CLM-1", idempotency_key="pay-1"
    )
    again = await core.money.create_payment_instruction(
        payee=PAYEE, amount=Decimal("1500.00"), claim_number="CLM-1", idempotency_key="pay-1"
    )
    second = await core.money.create_payment_instruction(
        payee=PAYEE, amount=Decimal("1500.00"), claim_number="CLM-1", idempotency_key="pay-2"
    )

    assert again == first
    assert second.payment_id != first.payment_id
    assert first.amount == Decimal("1500.00")


async def test_permissions_follow_seeded_grants(core: Core) -> None:
    perm = core.permission

    assert await perm.can_submit_for(
        user_id=MEMBER_USER_ID, policy_number=MEMBER_POLICY_NUMBER, hospital_id=None
    )
    assert not await perm.can_submit_for(
        user_id=MEMBER_USER_ID, policy_number="MOCK/AS/2026/000102", hospital_id=None
    )
    assert await perm.can_submit_for(
        user_id=HOSPITAL_USER_ID, policy_number=None, hospital_id=HOSPITAL_ROHINI_IDS[0]
    )
    assert not await perm.can_submit_for(
        user_id=HOSPITAL_USER_ID, policy_number=None, hospital_id=HOSPITAL_ROHINI_IDS[1]
    )
