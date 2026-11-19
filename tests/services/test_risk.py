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
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import GuidelineUsed, MedicalFindings
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.contract import (
    InvalidVerdict,
    NotReadyForRisk,
    RiskConfig,
    RiskReport,
    RiskService,
    Verdict,
    load_risk_config,
)
from app.services.risk.service import DefaultRiskService
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import Stage
from tests.database.factories import add_claim
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import (
    ClaimSpec,
    add_coding,
    add_digitised_claim,
    add_sheet,
    reviewed_bill,
    without_agreements,
)
from tests.services.reference_rows import add_reference_rows
from tests.services.test_registration import asha_policy

EXPLAINED = {"text": "Explained by the model."}
CONFIG = load_risk_config(Settings().config_dir / "risk.yaml")
REFER_AT = CONFIG.refer_at
SMALL = Decimal("48250.00")  # below half the sum insured and below the value threshold


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

    def service(self, gateway: FakeModelGateway, config: RiskConfig = CONFIG) -> RiskService:
        return DefaultRiskService(
            self.sessions,
            gateway,
            self.policies,
            kb=DefaultKnowledgeBase(self.sessions),
            config=config,
        )


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


async def test_a_high_claim_scores_below_the_threshold_but_is_referred_on_value(
    env: Env,
) -> None:
    claim_id = await env.claim()  # Rs 1,70,000 against a Rs 3,00,000 sum insured
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF2"]
    assert report.score == 30 and report.score < REFER_AT
    assert (report.refer, report.refer_reasons) == (True, ["value"])
    assert report.explanation == "Explained by the model."
    assert [c.role.value for c in gateway.calls] == ["fast_text"]


async def test_a_claim_below_the_value_threshold_is_not_referred_on_value(env: Env) -> None:
    raised = CONFIG.model_copy(update={"investigate_from_amount": Decimal("200000")})
    claim_id = await env.claim()
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway, raised).score(claim_id)

    assert (report.score, report.refer, report.refer_reasons) == (30, False, [])


async def test_an_early_high_claim_is_referred(env: Env) -> None:
    env.policies.policies = [asha_policy(inception_date=date(2026, 10, 20))]
    claim_id = await env.claim()
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF1", "RF2"]
    assert "13 days" in report.hits[0].detail
    assert (report.score, report.refer) == (70, True)
    assert report.refer_reasons == ["score", "value"]


EARLIER = {"admission_date": date(2026, 9, 1), "discharge_date": date(2026, 9, 4)}


async def test_a_file_reused_on_another_claim_is_referred_on_its_own(env: Env) -> None:
    await env.claim(ClaimSpec(claimed=Decimal("48250.00"), **EARLIER), file_hash="a" * 64)
    claim_id = await env.claim(ClaimSpec(claimed=Decimal("48250.00")), file_hash="a" * 64)
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway).score(claim_id)

    assert rule_ids(report) == ["RF3"]
    assert (report.score, report.refer) == (60, True)


async def test_the_score_is_capped_at_100(env: Env) -> None:
    env.policies.policies = [asha_policy(inception_date=date(2026, 10, 20))]
    await env.claim(ClaimSpec(**EARLIER), file_hash="b" * 64)
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
    await env.claim(ClaimSpec(claimed=Decimal("48250.00"), **EARLIER), file_hash="b" * 64)
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


# --- P2-6: the full red-flag set, the outlier score and the weights ------------------------


async def test_weights_and_the_threshold_come_from_config(env: Env) -> None:
    claim_id = await env.claim()
    heavier = CONFIG.model_copy(
        update={"weights": {**CONFIG.weights, "RF2": 65}, "investigate_from_amount": D9}
    )
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    report = await env.service(gateway, heavier).score(claim_id)

    assert (report.score, report.refer, report.refer_reasons) == (65, True, ["score"])
    assert report.config_version == CONFIG.version


D9 = Decimal("999999999")


async def test_a_claim_close_to_the_policy_expiry_is_flagged(env: Env) -> None:
    env.policies.policies = [asha_policy(policy_end=date(2026, 11, 20))]
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF4"]
    assert "18 days" in report.hits[0].detail


async def test_an_overlapping_stay_on_another_claim_is_flagged(env: Env) -> None:
    await env.claim(ClaimSpec(claimed=SMALL))
    claim_id = await env.claim(
        ClaimSpec(claimed=SMALL, admission_date=date(2026, 11, 4), discharge_date=date(2026, 11, 7))
    )

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF6"]


async def test_the_same_diagnosis_repeated_at_one_hospital_is_flagged(env: Env) -> None:
    for day in (1, 8, 15):  # three earlier stays, none overlapping
        other = await env.claim(
            ClaimSpec(claimed=SMALL, admission_date=date(2026, 10, day),
                      discharge_date=date(2026, 10, day + 2))
        )  # fmt: skip
        await add_coding(env.sessions, other)
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))
    await add_coding(env.sessions, claim_id)

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF5"]
    assert "K35" in report.hits[0].detail and "3 other claims" in report.hits[0].detail


async def test_a_sudden_jump_in_a_hospitals_volume_is_flagged(env: Env) -> None:
    for day in (1, 5, 9, 13):
        await env.claim(
            ClaimSpec(claimed=SMALL, admission_date=date(2026, 10, day),
                      discharge_date=date(2026, 10, day + 2))
        )  # fmt: skip
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF7"]
    assert "5 claims" in report.hits[0].detail


async def test_a_room_above_the_city_tiers_norm_is_flagged(env: Env) -> None:
    """Sunrise is in New Delhi, a CGHS X-tier city: Rs 7,500 a day against a Rs 5,000 norm."""
    await add_reference_rows(env.sessions)
    await without_agreements(env.sessions)
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))
    await reviewed_bill(env.sessions, claim_id)

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF8"]
    assert "7,500.00" in report.hits[0].detail and "X" in report.hits[0].detail


async def test_a_bill_not_on_letterhead_is_flagged(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(claimed=SMALL, bill_on_letterhead=False))

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF9"]


@pytest.mark.parametrize(
    ("spec", "words"),
    [
        (ClaimSpec(claimed=SMALL, dob=date(1990, 1, 1)), "date of birth"),
        (ClaimSpec(claimed=SMALL, gender="Male"), "gender"),
    ],
    ids=["age", "gender"],
)
async def test_an_identity_mismatch_is_flagged(env: Env, spec: ClaimSpec, words: str) -> None:
    claim_id = await env.claim(spec)

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF10"]
    assert words in report.hits[0].detail


async def test_the_outlier_score_compares_the_bill_and_the_stay_with_their_baselines(
    env: Env,
) -> None:
    """Rs 48,250 billed against the AG095 rate of Rs 33,000 (ratio 1.46, below 1.5) and a
    7-day stay against a 3-day norm (4 days over, at least 2): RF12 only. The outlier score
    is 4 whole tenths above the rate x 5 points, plus 4 days x 10 points."""
    claim_id = await env.claim(
        ClaimSpec(claimed=SMALL, admission_date=date(2026, 11, 2), discharge_date=date(2026, 11, 9))
    )
    await add_coding(env.sessions, claim_id)
    await add_sheet(env.sessions, claim_id)
    await add_findings(env.sessions, claim_id, los_norm_days=3)

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF12"]
    assert report.outlier is not None
    assert (report.outlier.package_rate, report.outlier.tariff_ratio) == (
        Decimal("33000.00"),
        Decimal("1.46"),
    )
    assert (report.outlier.stay_days, report.outlier.los_norm_days) == (7, 3)
    assert report.outlier.score == 4 * 5 + 4 * 10


async def test_a_bill_far_above_the_package_rate_is_a_tariff_outlier(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(claimed=Decimal("99000.00")))
    await add_coding(env.sessions, claim_id)
    await add_sheet(env.sessions, claim_id)

    report = await env.service(FakeModelGateway({"risk_explanation": [EXPLAINED]})).score(claim_id)

    assert rule_ids(report) == ["RF11"]
    assert report.outlier is not None and report.outlier.tariff_ratio == Decimal("3.00")


async def test_the_model_sees_only_the_hits_and_writes_only_text(env: Env) -> None:
    env.policies.policies = [asha_policy(policy_end=date(2026, 11, 20))]
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))
    gateway = FakeModelGateway({"risk_explanation": [EXPLAINED]})

    await env.service(gateway).score(claim_id)

    [call] = gateway.calls
    prompt = " ".join(m.content for m in call.messages if isinstance(m.content, str))
    assert "RF4" in prompt and "close to the policy" in prompt
    assert "Asha" not in prompt and "MOCK/AS" not in prompt  # no claim data beyond the hits
    assert call.schema is not None
    assert set(call.schema.model_json_schema()["properties"]) == {"text"}


async def test_scoring_reruns_when_the_bill_review_changes(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(claimed=SMALL))
    service = env.service(FakeModelGateway({}))
    first = await service.score(claim_id)
    await add_coding(env.sessions, claim_id)
    await add_sheet(env.sessions, claim_id)

    second = await service.score(claim_id)

    assert (first.bill_review_version, second.bill_review_version) == (None, 1)


async def add_findings(
    sessions: async_sessionmaker[AsyncSession], claim_id: UUID, *, los_norm_days: int
) -> None:
    findings = MedicalFindings(
        claim_id=claim_id,
        digitisation_version=1,
        registration_version=1,
        guideline=GuidelineUsed(
            package_code="AG095",
            guideline_code="SG017B",
            los_norm_days=los_norm_days,
            los_norm_source="PMJAY SG017B",
        ),
        verified_codes=[],
        findings=[],
        query_items=[],
        rule_results=[],
        lookups=[],
        dropped=[],
        revisions=0,
        adverse=False,
    )
    await StageRecordRepository(sessions).append(
        claim_id, Stage.MEDICAL_REVIEW, findings.model_dump(mode="json"), created_by="test"
    )
