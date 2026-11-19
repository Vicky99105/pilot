"""Registration through its contract, with fake policy and money services and the real
rules engine and product config."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.integrations.core.contract import InsuredMember, MemberPolicy, PremiumPayment
from app.services.registration.contract import (
    InvalidOverride,
    NotDigitised,
    RegistrationOverrides,
    RegistrationResult,
    RegistrationService,
)
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS, PRODUCT_UIN
from app.shared.types import RuleResult, Stage
from tests.database.factories import add_claim
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.services.claim_builders import MEMBER, POLICY, ClaimSpec, add_digitised_claim

D = Decimal


def asha_policy(**changes: object) -> MemberPolicy:
    policy = MemberPolicy(
        policy_number=POLICY,
        product_uin=PRODUCT_UIN,
        sum_insured=D("300000.00"),
        cumulative_bonus=D("0.00"),
        utilised=D("0.00"),
        blocked=D("0.00"),
        inception_date=date(2024, 4, 1),
        continuous_cover_start=date(2024, 4, 1),
        policy_start=date(2026, 4, 1),
        policy_end=date(2027, 3, 31),
        premium_payments=[
            PremiumPayment(paid_on=date(2026, 3, 29), amount=D("9000"), status="received")
        ],
        declared_peds=[],
        insured_members=[
            InsuredMember(
                member_id=MEMBER,
                name="Asha Verma",
                dob=date(1984, 6, 12),
                gender="F",
                relationship="self",
            ),
            InsuredMember(
                member_id="MOCK-M-000101-02",
                name="Rohit Verma",
                dob=date(1981, 2, 3),
                gender="M",
                relationship="spouse",
            ),
        ],
    )
    return policy.model_copy(update=changes)


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    policies: FakePolicyService
    money: FakeMoneyService
    service: RegistrationService

    async def claim(self, spec: ClaimSpec | None = None) -> UUID:
        return await add_digitised_claim(self.sessions, spec)

    async def claim_row(self, claim_id: UUID) -> ClaimRow:
        async with self.sessions() as session:
            row = await session.get(ClaimRow, claim_id)
        assert row is not None
        return row


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    policies, money = FakePolicyService([asha_policy()]), FakeMoneyService()
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    return Env(
        sessions, policies, money, DefaultRegistrationService(sessions, policies, money, rules)
    )


def rule(result: RegistrationResult, rule_id: str) -> RuleResult:
    return next(r for r in result.rule_results if r.rule_id == rule_id)


async def test_a_clean_claim_registers_blocks_and_gets_a_claim_number(env: Env) -> None:
    claim_id = await env.claim()

    result = await env.service.register(claim_id)

    assert [r.rule_id for r in result.rule_results] == ["R3", "R1", "R2", "R5", "R10"]
    assert all(r.passed for r in result.rule_results), result.rule_results
    assert (result.member.match, result.member.member_id) == ("exact_id", MEMBER)
    assert (result.hospital.match, result.hospital.rohini_id) == (
        "rohini_id",
        HOSPITAL_ROHINI_IDS[0],
    )
    assert result.policy is not None and result.policy.balance == D("300000.00")
    assert result.block_amount == D("170000.00")
    assert result.block_ref is not None and result.block_ref.amount == D("170000.00")
    assert re.fullmatch(r"CLM-\d{4}-\d{6}", result.claim_number)
    assert result.duplicates == []
    assert (await env.claim_row(claim_id)).claim_number == result.claim_number
    stored = await StageRecordRepository(env.sessions).history(claim_id, Stage.REGISTRATION)
    assert [(r.version, r.created_by) for r in stored] == [(1, "code")]


async def test_registration_is_idempotent_and_reruns_return_the_same_block(env: Env) -> None:
    claim_id = await env.claim()

    first = await env.service.register(claim_id)
    again = await env.service.register(claim_id)

    assert again == first
    assert len(env.money.blocks) == 1
    assert list(env.money.blocks) == [f"{claim_id}:block:1"]


async def test_a_block_made_before_a_crash_is_reused_on_the_rerun(env: Env) -> None:
    claim_id = await env.claim()
    # The block call succeeded but the result was never stored.
    earlier = await env.money.block_sum_insured(
        policy_number=POLICY,
        member_id=MEMBER,
        amount=D("170000.00"),
        idempotency_key=f"{claim_id}:block:1",
    )

    result = await env.service.register(claim_id)

    assert result.block_ref == earlier
    assert len(env.money.blocks) == 1


async def test_claim_numbers_increase_per_claim(env: Env) -> None:
    first = await env.service.register(await env.claim())
    second = await env.service.register(
        await env.claim(ClaimSpec(admission_date=date(2026, 9, 1), discharge_date=date(2026, 9, 3)))
    )

    assert int(second.claim_number[-6:]) == int(first.claim_number[-6:]) + 1


async def test_a_doubtful_member_fails_r3_and_blocks_nothing(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(member_id="MOCK-M-000101-09", dob=date(1985, 1, 1)))

    result = await env.service.register(claim_id)

    assert result.member.match == "doubtful"
    assert result.member.member_id is None
    r3 = rule(result, "R3")
    assert not r3.passed and "doubtful match" in r3.reason and r3.clause_ref == "INSURED"
    assert result.block_ref is None
    assert env.money.blocks == {}


async def test_a_person_can_override_the_member_match_as_a_new_version(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(member_id="MOCK-M-000101-09", dob=date(1985, 1, 1)))
    await env.service.register(claim_id)

    result = await env.service.apply_overrides(
        claim_id,
        RegistrationOverrides(member_id=MEMBER, reason="KYC shows DOB typo on the claim form"),
        actor="task:abc",
    )

    assert (result.member.match, result.member.member_id) == ("person", MEMBER)
    r3 = rule(result, "R3")
    assert (r3.passed, r3.judged_by) == (True, "person")
    assert "KYC shows DOB typo" in r3.reason
    assert result.block_ref is not None
    history = await StageRecordRepository(env.sessions).history(claim_id, Stage.REGISTRATION)
    assert [(r.version, r.created_by) for r in history] == [(1, "code"), (2, "task:abc")]
    assert list(env.money.blocks) == [f"{claim_id}:block:2"]


@pytest.mark.parametrize(
    "overrides",
    [
        RegistrationOverrides(member_id="NOT-ON-POLICY", reason="x"),
        RegistrationOverrides(hospital_id="0000000000000", reason="x"),
        RegistrationOverrides(member_id=MEMBER, reason="   "),
        RegistrationOverrides(rules={"R99": "x"}),
        RegistrationOverrides(rules={"R5": ""}),
    ],
    ids=["unknown_member", "unknown_hospital", "no_reason", "unknown_rule", "empty_rule_reason"],
)
async def test_invalid_overrides_are_refused(env: Env, overrides: RegistrationOverrides) -> None:
    claim_id = await env.claim()
    await env.service.register(claim_id)

    with pytest.raises(InvalidOverride):
        await env.service.apply_overrides(claim_id, overrides, actor="task:abc")


async def test_a_person_can_accept_a_failed_rule_with_a_reason(env: Env) -> None:
    recent = date(2026, 10, 25)
    env.policies.policies = [asha_policy(inception_date=recent, continuous_cover_start=recent)]
    claim_id = await env.claim()
    before = await env.service.register(claim_id)

    after = await env.service.apply_overrides(
        claim_id, RegistrationOverrides(rules={"R5": "ported policy, wait served"}), actor="p"
    )

    assert not rule(before, "R5").passed
    assert (rule(after, "R5").passed, rule(after, "R5").judged_by) == (True, "person")


async def test_hospital_matches_by_name_and_city_without_a_rohini_id(env: Env) -> None:
    claim_id = await env.claim(
        ClaimSpec(hospital_id=None, hospital_name="SAHYADRI NURSING HOME.", hospital_city="pune")
    )

    result = await env.service.register(claim_id)

    assert (result.hospital.match, result.hospital.rohini_id) == (
        "name_city",
        HOSPITAL_ROHINI_IDS[3],
    )
    assert result.hospital.differential_billing is False
    assert (await env.claim_row(claim_id)).hospital_id == HOSPITAL_ROHINI_IDS[3]


async def test_an_unknown_hospital_is_left_unmatched(env: Env) -> None:
    claim_id = await env.claim(
        ClaimSpec(hospital_id=None, hospital_name="Nowhere Clinic", hospital_city="Pune")
    )

    result = await env.service.register(claim_id)

    assert result.hospital.match == "none"
    assert result.hospital.differential_billing is True


async def test_an_overlapping_claim_for_the_same_member_is_a_duplicate(env: Env) -> None:
    earlier = await env.claim(
        ClaimSpec(admission_date=date(2026, 11, 4), discharge_date=date(2026, 11, 8))
    )
    await env.claim(ClaimSpec(admission_date=date(2026, 11, 20), discharge_date=date(2026, 11, 22)))
    await env.claim(ClaimSpec(member_id="MOCK-M-000101-02", patient_name="Rohit Verma"))
    claim_id = await env.claim()

    result = await env.service.register(claim_id)

    assert [d.claim_id for d in result.duplicates] == [earlier]
    assert result.duplicates[0].admission_date == date(2026, 11, 4)


async def test_insufficient_sum_insured_fails_r10_and_blocks_nothing(env: Env) -> None:
    env.policies.policies = [asha_policy(utilised=D("150000"), blocked=D("40000"))]
    claim_id = await env.claim()

    result = await env.service.register(claim_id)

    r10 = rule(result, "R10")
    assert not r10.passed and "Rs 110,000.00" in r10.reason
    assert result.block_ref is None


async def test_the_accident_flag_from_the_record_exempts_the_initial_wait(env: Env) -> None:
    recent = date(2026, 10, 25)
    env.policies.policies = [asha_policy(inception_date=recent, continuous_cover_start=recent)]
    illness = await env.service.register(await env.claim())
    accident = await env.service.register(
        await env.claim(
            ClaimSpec(
                accident=True,
                admission_date=date(2026, 11, 12),
                discharge_date=date(2026, 11, 14),
            )
        )
    )

    assert (rule(illness, "R5").passed, rule(illness, "R5").clause_ref) == (False, "Excl03")
    assert rule(accident, "R5").passed


async def test_an_unknown_policy_fails_every_rule(env: Env) -> None:
    env.policies.policies = []
    claim_id = await env.claim()

    result = await env.service.register(claim_id)

    assert result.policy is None
    assert all(not r.passed for r in result.rule_results)
    assert result.block_ref is None


async def test_a_new_record_version_releases_the_old_block_and_blocks_again(env: Env) -> None:
    claim_id = await env.claim()
    first = await env.service.register(claim_id)
    record = await StageRecordRepository(env.sessions).latest(claim_id, Stage.DIGITISATION)
    assert record is not None
    payload = record.payload | {"corrected_fields": ["claimed.total"]}
    payload["claimed"]["total"]["value"] = "160000.00"
    await StageRecordRepository(env.sessions).append(
        claim_id, Stage.DIGITISATION, payload, created_by="task:x"
    )

    second = await env.service.register(claim_id)

    assert second.digitisation_version == 2
    assert second.claim_number == first.claim_number
    assert second.block_ref is not None and second.block_ref.amount == D("160000.00")
    assert first.block_ref is not None
    assert [b for b, _ in env.money.releases.values()] == [first.block_ref.block_id]


async def test_register_needs_a_digitised_claim(env: Env) -> None:
    with pytest.raises(NotDigitised):
        await env.service.register(await add_claim(env.sessions))
    with pytest.raises(NotDigitised):
        await env.service.register(uuid4())
