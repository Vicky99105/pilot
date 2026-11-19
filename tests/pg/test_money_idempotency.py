"""The mock core's money calls on real Postgres: concurrent first calls with one idempotency
key race on the key's primary key. Exactly one changes money; every caller gets its result."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.mock_core.models import McBlock, McIdempotency, McPayment, McPolicy
from app.mock_core.router import router
from app.mock_core.seed import seed_mock_core
from app.shared.seed_ids import MEMBER_MEMBER_ID, MEMBER_POLICY_NUMBER

pytestmark = pytest.mark.pg

CALLERS = 12


@pytest.fixture
async def core(
    empty_database: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(empty_database, pool_size=CALLERS + 2)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await seed_mock_core(engine, sessions)
    app = FastAPI()
    app.include_router(router, prefix="/mock-core")
    app.state.mock_core_sessions = sessions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
        yield client, sessions
    await engine.dispose()


async def _count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_concurrent_payments_with_one_key_pay_once(
    core: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, sessions = core
    body = {
        "payee": {"kind": "member", "name": "ASHA", "account_number": "1", "ifsc": "SBIN0000001"},
        "amount": "62924.52",
        "claim_number": "CLM-1",
        "idempotency_key": "claim-1-pay-1",
    }

    responses = await asyncio.gather(
        *(client.post("/mock-core/payments", json=body) for _ in range(CALLERS))
    )

    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["payment_id"] for r in responses}) == 1
    assert await _count(sessions, McPayment) == 1
    assert await _count(sessions, McIdempotency) == 1


async def test_concurrent_blocks_with_one_key_block_once(
    core: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, sessions = core
    async with sessions() as session:
        policy = await session.get(McPolicy, MEMBER_POLICY_NUMBER)
        assert policy is not None
        blocked_before = policy.blocked
    body = {
        "policy_number": MEMBER_POLICY_NUMBER,
        "member_id": MEMBER_MEMBER_ID,
        "amount": "10000.00",
        "idempotency_key": "claim-1-block-1",
    }

    responses = await asyncio.gather(
        *(client.post("/mock-core/blocks", json=body) for _ in range(CALLERS))
    )

    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["block_id"] for r in responses}) == 1
    assert await _count(sessions, McBlock) == 1
    async with sessions() as session:
        policy = await session.get(McPolicy, MEMBER_POLICY_NUMBER)
        assert policy is not None
        assert policy.blocked - blocked_before == Decimal("10000.00")
