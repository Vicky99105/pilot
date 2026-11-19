"""Risk scoring through its contract: red flags and the score in code, the explanation from
a scripted model gateway."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow, FileRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.contract import (
    REFER_AT,
    InvalidVerdict,
    NotReadyForRisk,
    RiskReport,
    RiskService,
    Verdict,
)
from app.services.risk.service import DefaultRiskService
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import Stage
from tests.database.factories import add_claim
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import ClaimSpec, add_digitised_claim
from tests.services.test_registration import asha_policy

EXPLAINED = {"text": "Explained by the model."}


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    policies: FakePolicyService

    async def claim(self, spec: ClaimSpec | None = None, file_hash: str | None = None) -> UUID:
        claim_id = await add_digitised_claim(self.sessions, spec)
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        await DefaultRegistrationService(
            self.sessions, self.policies, FakeMoneyService(), rules
        ).register(claim_id)
        if file_hash:
            await add_file(self.sessions, claim_id, file_hash)
        return claim_id

    def service(self, gateway: FakeModelGateway) -> RiskService:
        return DefaultRiskService(self.sessions, gateway, self.policies)


async def add_file(sessions: async_sessionmaker[AsyncSession], claim_id: UUID, sha: str) -> None:
    async with sessions.begin() as session:
        claim = await session.scalar(select(ClaimRow).where(ClaimRow.id == claim_id))
        assert claim is not None and claim.submission_id is not None
        session.add(
            FileRow(
                id=uuid4(),
                submission_id=claim.submission_id,
                sha256=sha,
                mime="application/pdf",
                size_bytes=10,
                storage_key=str(uuid4()),
                created_at=datetime.now(UTC),
            )
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions, FakePolicyService([asha_policy()]))


def rule_ids(report: RiskReport) -> list[str]:
    return [h.rule_id for h in report.hits]


async def test_a_clean_claim_scores_zero_without_a_model_call(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(claimed=Decimal("48250.00")))
    gateway = FakeModelGateway({})

    report = await env.service(gateway).score(claim_id)

    assert (report.hits, report.score, report.refer) == ([], 0, False)
    assert report.explanation == "No red flags."
    assert gateway.calls == []


async def test_a_high_claim_alone_scores_below_the_referral_threshold(env: Env) -> None:
    claim_id = await env.claim()  # Rs 1,70,000 against a Rs 3,00,000 sum insured
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF2"]
    assert report.score == 30 and report.score < REFER_AT
    assert report.refer is False
    assert report.explanation == "Explained by the model."
    assert [c.role.value for c in gateway.calls] == ["fast_text"]


async def test_an_early_high_claim_is_referred(env: Env) -> None:
    env.policies.policies = [asha_policy(inception_date=date(2026, 10, 20))]
    claim_id = await env.claim()
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF1", "RF2"]
    assert "13 days" in report.hits[0].detail
    assert (report.score, report.refer) == (70, True)


async def test_a_file_reused_on_another_claim_is_referred_on_its_own(env: Env) -> None:
    await env.claim(ClaimSpec(claimed=Decimal("48250.00")), file_hash="a" * 64)
    claim_id = await env.claim(ClaimSpec(claimed=Decimal("48250.00")), file_hash="a" * 64)
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF3"]
    assert (report.score, report.refer) == (60, True)


async def test_the_score_is_capped_at_100(env: Env) -> None:
    env.policies.policies = [asha_policy(inception_date=date(2026, 10, 20))]
    await env.claim(file_hash="b" * 64)
    claim_id = await env.claim(file_hash="b" * 64)
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF1", "RF2", "RF3"]
    assert report.score == 100


async def test_scoring_is_idempotent_per_registration_version(env: Env) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})
    service = env.service(gateway)

    assert await service.score(claim_id) == await service.score(claim_id)
    assert len(gateway.calls) == 1


async def test_a_claim_without_registration_is_not_ready(env: Env) -> None:
    claim_id = await add_claim(env.sessions)

    with pytest.raises(NotReadyForRisk):
        await env.service(FakeModelGateway({})).score(claim_id)


# --- P2-1: the investigator's verdict takes effect -----------------------------------------


async def _referred(env: Env) -> tuple[RiskService, UUID]:
    spec = ClaimSpec(claimed=Decimal("48250.00"))
    await env.claim(spec, file_hash="b" * 64)
    claim_id = await env.claim(spec, file_hash="b" * 64)
    service = env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]}))
    await service.score(claim_id)
    return service, claim_id


@pytest.mark.parametrize("verdict", ["cleared", "fraud_confirmed"])
async def test_a_verdict_writes_a_new_risk_report_version(env: Env, verdict: Verdict) -> None:
    service, claim_id = await _referred(env)
    scored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.RISK)
    assert scored is not None

    report = await service.record_verdict(
        claim_id, verdict, "  Bills checked with the hospital  ", actor="user:inv"
    )

    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.RISK)
    assert stored is not None and stored.version == scored.version + 1
    assert stored.created_by == "user:inv"
    assert (report.verdict, report.verdict_note) == (verdict, "Bills checked with the hospital")
    assert report.verdict_by == "user:inv"
    assert (report.score, report.refer) == (60, True)  # the score itself is unchanged
    # A repeat score keeps the verdict: the registration did not change.
    assert await service.score(claim_id) == report


async def test_a_verdict_needs_a_note_and_a_risk_report(env: Env) -> None:
    service, claim_id = await _referred(env)

    with pytest.raises(InvalidVerdict, match="note"):
        await service.record_verdict(claim_id, "cleared", " ", actor="user:inv")
    with pytest.raises(InvalidVerdict, match="risk report"):
        await service.record_verdict(uuid4(), "cleared", "note", actor="user:inv")
