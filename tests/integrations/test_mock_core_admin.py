"""The mock core's admin reset of one policy's balances: only with the explicit local
setting on, mock data only, and recorded as a monitoring event."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.integrations.core.http import HttpMoneyService
from app.main import create_app
from app.mock_core.models import McBlock, McPolicy
from app.mock_core.router import router as mock_core_router
from app.mock_core.seed import seed_mock_core
from app.services.monitoring.contract import SYSTEM_EVENTS
from app.shared.errors import PermanentError
from app.shared.seed_ids import MEMBER_MEMBER_ID, MEMBER_POLICY_NUMBER
from app.shared.types import Stage
from tests.fakes.monitoring import FakeMonitoring

D = Decimal
RESET = "/mock-core/admin/reset-policy"
REPO = Path(__file__).resolve().parents[2]


@dataclass
class Core:
    http: httpx.AsyncClient
    money: HttpMoneyService
    sessions: async_sessionmaker[AsyncSession]
    monitoring: FakeMonitoring

    async def policy(self) -> McPolicy:
        async with self.sessions() as session:
            policy = await session.get(McPolicy, MEMBER_POLICY_NUMBER)
        assert policy is not None
        return policy


async def _core(tmp_path: Path, enabled: bool | None) -> AsyncIterator[Core]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mock_core.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await seed_mock_core(engine, sessions)
    app = FastAPI()
    app.state.mock_core_sessions = sessions
    app.state.monitoring = monitoring = FakeMonitoring()
    if enabled is not None:  # None: an app that never set it
        app.state.mock_core_admin_enabled = enabled
    app.include_router(mock_core_router, prefix="/mock-core")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app")
    yield Core(http, HttpMoneyService("http://app/mock-core", http), sessions, monitoring)
    await http.aclose()
    await engine.dispose()


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Core]:
    async for value in _core(tmp_path, enabled=True):
        yield value


@pytest.fixture(params=[False, None], ids=["off", "never_set"])
async def core_off(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Core]:
    async for value in _core(tmp_path, enabled=request.param):
        yield value


async def _use_the_policy(core: Core) -> str:
    """A released claim (utilised Rs 31,350) and an open block (Rs 93,267)."""
    member = {"policy_number": MEMBER_POLICY_NUMBER, "member_id": MEMBER_MEMBER_ID}
    done = await core.money.block_sum_insured(**member, amount=D("40000"), idempotency_key="k1")
    await core.money.release_block(block_id=done.block_id, utilised=D("31350"),
                                   idempotency_key="k2")  # fmt: skip
    open_block = await core.money.block_sum_insured(**member, amount=D("93267"),
                                                    idempotency_key="k3")  # fmt: skip
    return open_block.block_id


async def test_reset_is_refused_unless_the_setting_is_on(core_off: Core) -> None:
    await _use_the_policy(core_off)

    response = await core_off.http.post(
        RESET, json={"policy_number": MEMBER_POLICY_NUMBER}, headers={"X-User-Id": "acceptance"}
    )

    assert response.status_code == 403
    assert "MOCK_CORE_ADMIN_ENABLED" in response.json()["detail"]
    policy = await core_off.policy()
    assert (policy.utilised, policy.blocked) == (D("31350.00"), D("93267.00"))
    assert core_off.monitoring.recorded == []


async def test_reset_clears_one_policys_utilisation_and_blocks(core: Core) -> None:
    open_block = await _use_the_policy(core)

    response = await core.http.post(
        RESET, json={"policy_number": MEMBER_POLICY_NUMBER}, headers={"X-User-Id": "acceptance"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "policy_number": MEMBER_POLICY_NUMBER,
        "utilised_before": "31350.00",
        "blocked_before": "93267.00",
        "blocks_voided": 1,
    }
    policy = await core.policy()
    assert (policy.utilised, policy.blocked) == (D("0.00"), D("0.00"))
    async with core.sessions() as session:
        statuses = set(await session.scalars(select(McBlock.status)))
    assert statuses == {"released", "voided"}
    # A voided block can no longer be released, so it never takes the balance below zero.
    with pytest.raises(PermanentError, match="not active"):
        await core.money.release_block(block_id=open_block, utilised=D("100"), idempotency_key="k4")
    assert ((await core.policy()).utilised, (await core.policy()).blocked) == (D("0"), D("0"))


async def test_reset_is_recorded_as_a_monitoring_event(core: Core) -> None:
    await _use_the_policy(core)

    await core.http.post(
        RESET, json={"policy_number": MEMBER_POLICY_NUMBER}, headers={"X-User-Id": "acceptance"}
    )

    [event] = core.monitoring.recorded
    assert (event.claim_id, event.stage, event.action, event.actor) == (
        SYSTEM_EVENTS,
        Stage.MONITORING,
        "mock_core_policy_reset",
        "acceptance",
    )
    assert event.detail == {
        "policy_number": MEMBER_POLICY_NUMBER,
        "utilised_before": "31350.00",
        "blocked_before": "93267.00",
        "blocks_voided": 1,
    }


async def test_reset_of_an_unknown_policy_is_not_found(core: Core) -> None:
    response = await core.http.post(RESET, json={"policy_number": "NOPE"})

    assert response.status_code == 404
    assert core.monitoring.recorded == []


def test_the_setting_is_off_by_default_and_on_only_in_the_local_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOCK_CORE_ADMIN_ENABLED", raising=False)
    assert Settings().mock_core_admin_enabled is False
    assert create_app(Settings()).state.mock_core_admin_enabled is False
    monkeypatch.setenv("MOCK_CORE_ADMIN_ENABLED", "true")
    assert create_app(Settings()).state.mock_core_admin_enabled is True

    def app_env(name: str) -> dict[str, str]:
        compose = yaml.safe_load((REPO / "deploy" / name).read_text())
        return compose["services"]["app"].get("environment", {}) or {}

    assert "MOCK_CORE_ADMIN_ENABLED" not in app_env("docker-compose.yml")  # production
    assert app_env("docker-compose.local.yml")["MOCK_CORE_ADMIN_ENABLED"] == "true"
