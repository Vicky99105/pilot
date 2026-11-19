"""Default registration: policy lookup, eligibility rules, hospital match, duplicate check,
sum insured block and claim number. Every rule runs in the rules engine; this service only
gathers facts and stores the result."""

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, HospitalRow, SubmissionRow, utcnow
from app.database.repositories.stage_records import (
    StageRecordRepository,
    append_in_session,
)
from app.integrations.core.contract import MemberPolicy, MoneyService, PolicyService
from app.services.digitisation.contract import ClaimRecord
from app.services.intake.contract import ClaimForm
from app.services.registration._helpers import claim_number, names_match, normalise, overlaps
from app.services.registration.contract import (
    REGISTRATION_RULES,
    DuplicateClaim,
    InvalidOverride,
    MatchedHospital,
    MatchedMember,
    NotDigitised,
    PolicySummary,
    RegistrationOverrides,
    RegistrationResult,
)
from app.services.rules.contract import Claimant, Facts, MemberMatch, RulesEngine
from app.shared.types import CASHLESS_REQUESTS, RequestType, RuleResult, Stage

CENTS = Decimal("0.01")
CLAIM_NUMBER_ATTEMPTS = 3


class DefaultRegistrationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        policies: PolicyService,
        money: MoneyService,
        rules: RulesEngine,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._policies = policies
        self._money = money
        self._rules = rules
        self._now = now
        self._records = StageRecordRepository(sessions)

    async def register(self, claim_id: UUID) -> RegistrationResult:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        if digitised is None:
            raise NotDigitised(str(claim_id))
        latest = await self._records.latest(claim_id, Stage.REGISTRATION)
        if latest is not None and latest.payload["digitisation_version"] == digitised.version:
            return RegistrationResult.model_validate(latest.payload)

        record = ClaimRecord.model_validate(digitised.payload)
        claim, form = await self._claim(claim_id)
        claimant = Claimant(
            member_id=record.identity.member_id.value or claim.member_id,
            name=record.identity.patient_name.value or (form.patient_name if form else None),
            dob=record.identity.date_of_birth.value,
        )
        policy = await self._find_policy(
            record.identity.policy_number.value or claim.policy_number, claimant.member_id
        )
        admitted = record.admission.admission_date.value or (form.admission_date if form else None)
        discharged = record.admission.discharge_date.value or (
            form.discharge_date if form else None
        )
        amount = _amount(record, form)
        accident = bool(record.admission.accident.value)

        if policy is None:
            match = MemberMatch(kind="none", member=None, score=0.0, reason="no policy found")
            rule_results = [
                RuleResult(rule_id=r, passed=False, reason="no policy found for the claim")
                for r in REGISTRATION_RULES
            ]
        else:
            match = self._rules.match_member(claimant, policy.insured_members)
            facts = Facts(
                admission_date=admitted,
                accident=accident,
                policy=policy,
                claimant=claimant,
                amount=amount,
            )
            product = self._rules.product(policy.product_uin)
            rule_results = self._rules.evaluate(REGISTRATION_RULES, facts, product)

        member = MatchedMember(
            match=match.kind,
            member_id=match.member.member_id if match.member else None,
            name=match.member.name if match.member else None,
            dob=match.member.dob if match.member else None,
            relationship=match.member.relationship if match.member else None,
            reason=match.reason,
        )
        result = RegistrationResult(
            claim_id=claim_id,
            digitisation_version=digitised.version,
            claim_number=claim.claim_number or "",
            member=member,
            policy=_summary(policy) if policy else None,
            hospital=await self._match_hospital(
                claim.hospital_id or (form.hospital_id if form else None),
                record.hospital.name.value or (form.hospital_name if form else None),
                record.hospital.city.value,
            ),
            admission_date=admitted,
            accident=accident,
            block_amount=amount,
            rule_results=rule_results,
            duplicates=await self._duplicates(
                claim_id, member.member_id or claimant.member_id, admitted, discharged
            ),
            block_ref=None,
        )
        version = (latest.version if latest else 0) + 1
        previous = RegistrationResult.model_validate(latest.payload) if latest else None
        if (
            previous is not None
            and previous.block_ref is not None
            and RequestType(claim.request_type) in CASHLESS_REQUESTS
        ):
            # A later cashless request keeps the admission's block; its authorisation sets
            # the amount (an enhancement raises it). Never release and re-block here.
            result.block_ref, result.block_amount = previous.block_ref, previous.block_amount
            return await self._store(result, created_by="code")
        if previous is not None and previous.block_ref is not None:
            await self._money.release_block(
                block_id=previous.block_ref.block_id,
                utilised=Decimal("0"),
                idempotency_key=f"{claim_id}:release:{previous.block_ref.block_id}",
            )
        await self._block(result, version)
        return await self._store(result, created_by="code")

    async def apply_overrides(
        self, claim_id: UUID, overrides: RegistrationOverrides, *, actor: str
    ) -> RegistrationResult:
        latest = await self._records.latest(claim_id, Stage.REGISTRATION)
        if latest is None:
            raise InvalidOverride(f"claim {claim_id} has no registration to override")
        result = RegistrationResult.model_validate(latest.payload)
        if (overrides.member_id or overrides.hospital_id) and not overrides.reason:
            raise InvalidOverride("a member or hospital override needs a reason")
        if overrides.member_id:
            self._override_member(result, overrides.member_id, overrides.reason)
        if overrides.hospital_id:
            await self._override_hospital(result, overrides.hospital_id, overrides.reason)
        for rule_id, why in overrides.rules.items():
            _accept_rule(result, rule_id, why.strip())
        await self._block(result, latest.version + 1)
        return await self._store(result, created_by=actor)

    # --- Facts ---------------------------------------------------------------------------

    async def _claim(self, claim_id: UUID) -> tuple[ClaimRow, ClaimForm | None]:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
        if claim is None:
            raise NotDigitised(f"claim {claim_id} not found")
        intake = await self._records.latest(claim_id, Stage.INTAKE)
        form = ClaimForm.model_validate(intake.payload["form"]) if intake else None
        return claim, form

    async def _find_policy(
        self, policy_number: str | None, member_id: str | None
    ) -> MemberPolicy | None:
        """By policy and member first; then the policy alone, so a mistyped member ID can
        still match by name; then the member ID alone."""
        attempts = [(policy_number, member_id), (policy_number, None), (None, member_id)]
        for policy, member in attempts:
            if policy or member:
                found = await self._policies.find_member(policy_number=policy, member_id=member)
                if found is not None:
                    return found
        return None

    async def _match_hospital(
        self, rohini_id: str | None, name: str | None, city: str | None
    ) -> MatchedHospital:
        async with self._sessions() as session:
            if rohini_id:
                row = await session.get(HospitalRow, rohini_id)
                if row is not None:
                    return _hospital(row, "rohini_id")
            if name and city:
                rows = await session.scalars(
                    select(HospitalRow).where(func.lower(HospitalRow.city) == normalise(city))
                )
                for row in rows:
                    if names_match(name, row.name):
                        return _hospital(row, "name_city")
        return MatchedHospital(
            match="none", rohini_id=None, name=name, city=city, differential_billing=True
        )

    async def _duplicates(
        self,
        claim_id: UUID,
        member_id: str | None,
        admitted: date | None,
        discharged: date | None,
    ) -> list[DuplicateClaim]:
        if not member_id or admitted is None:
            return []
        async with self._sessions() as session:
            rows = await session.execute(
                select(ClaimRow, SubmissionRow)
                .join(SubmissionRow, SubmissionRow.id == ClaimRow.submission_id)
                .where(ClaimRow.member_id == member_id, ClaimRow.id != claim_id)
                .order_by(ClaimRow.created_at)
            )
            found = []
            for other, submission in rows:
                form = ClaimForm.model_validate(submission.form)
                if overlaps(admitted, discharged, form.admission_date, form.discharge_date):
                    found.append(
                        DuplicateClaim(
                            claim_id=other.id,
                            claim_number=other.claim_number,
                            admission_date=form.admission_date,
                            discharge_date=form.discharge_date,
                        )
                    )
        return found

    # --- Overrides -----------------------------------------------------------------------

    def _override_member(self, result: RegistrationResult, member_id: str, reason: str) -> None:
        members = result.policy.insured_members if result.policy else []
        member = next((m for m in members if m.member_id == member_id), None)
        if member is None:
            raise InvalidOverride(f"member {member_id} is not on the policy")
        result.member = MatchedMember(
            match="person",
            member_id=member.member_id,
            name=member.name,
            dob=member.dob,
            relationship=member.relationship,
            reason=reason,
        )
        _accept_rule(result, "R3", f"member set to {member_id} by a person: {reason}")
        result.overrides = [*result.overrides, f"member {member_id}: {reason}"]

    async def _override_hospital(
        self, result: RegistrationResult, rohini_id: str, reason: str
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(HospitalRow, rohini_id)
        if row is None:
            raise InvalidOverride(f"hospital {rohini_id} is not in the hospital list")
        result.hospital = _hospital(row, "person")
        result.overrides = [*result.overrides, f"hospital {rohini_id}: {reason}"]

    # --- Money and storage ---------------------------------------------------------------

    async def _block(self, result: RegistrationResult, version: int) -> None:
        """Block the claimed amount once the member is known and R10 passes. The key holds
        the record version, so a re-run of the same version gets the same block back."""
        r10 = next((r for r in result.rule_results if r.rule_id == "R10"), None)
        if (
            result.block_ref is not None
            or result.policy is None
            or result.member.member_id is None
            or result.block_amount is None
            or r10 is None
            or not r10.passed
        ):
            return
        result.block_ref = await self._money.block_sum_insured(
            policy_number=result.policy.policy_number,
            member_id=result.member.member_id,
            amount=result.block_amount,
            idempotency_key=f"{result.claim_id}:block:{version}",
        )

    async def _store(self, result: RegistrationResult, *, created_by: str) -> RegistrationResult:
        """Assign the claim number if the claim has none, then store the result. Two claims
        racing for one number fail on the unique constraint; the loser retries."""
        for attempt in range(CLAIM_NUMBER_ATTEMPTS):
            try:
                async with self._sessions.begin() as session:
                    claim = await session.get(ClaimRow, result.claim_id)
                    assert claim is not None
                    if claim.claim_number is None:
                        claim.claim_number = await self._next_claim_number(session)
                        await session.flush()
                    if result.hospital.rohini_id and result.hospital.match != "none":
                        claim.hospital_id = result.hospital.rohini_id
                    result.claim_number = claim.claim_number
                    await append_in_session(
                        session,
                        result.claim_id,
                        Stage.REGISTRATION,
                        result.model_dump(mode="json"),
                        created_by=created_by,
                    )
                return result
            except IntegrityError:
                if attempt == CLAIM_NUMBER_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    async def _next_claim_number(self, session: AsyncSession) -> str:
        year = self._now().year
        last = await session.scalar(
            select(ClaimRow.claim_number)
            .where(ClaimRow.claim_number.like(f"CLM-{year}-%"))
            .order_by(ClaimRow.claim_number.desc())
            .limit(1)
        )
        return claim_number(year, last)


def _amount(record: ClaimRecord, form: ClaimForm | None) -> Decimal | None:
    """The amount to block: the claimed total, else the bill total, else a cashless
    pre-auth's estimate, else the form."""
    for value in (
        record.claimed.total.value,
        record.bill.grand_total.value,
        record.preauth.estimate_total.value,
    ):
        if value is not None:
            return value.quantize(CENTS)
    return form.claimed_amount if form else None


def _summary(policy: MemberPolicy) -> PolicySummary:
    balance = policy.sum_insured + policy.cumulative_bonus - policy.utilised - policy.blocked
    return PolicySummary(
        policy_number=policy.policy_number,
        product_uin=policy.product_uin,
        sum_insured=policy.sum_insured,
        cumulative_bonus=policy.cumulative_bonus,
        utilised=policy.utilised,
        blocked=policy.blocked,
        balance=balance.quantize(CENTS),
        policy_start=policy.policy_start,
        policy_end=policy.policy_end,
        insured_members=policy.insured_members,
    )


def _hospital(row: HospitalRow, match: Any) -> MatchedHospital:
    return MatchedHospital(
        match=match,
        rohini_id=row.rohini_id,
        name=row.name,
        city=row.city,
        differential_billing=row.differential_billing,
    )


def _accept_rule(result: RegistrationResult, rule_id: str, reason: str) -> None:
    if not reason:
        raise InvalidOverride(f"accepting {rule_id} needs a reason")
    for i, r in enumerate(result.rule_results):
        if r.rule_id == rule_id:
            result.rule_results[i] = r.model_copy(
                update={"passed": True, "judged_by": "person", "reason": reason}
            )
            return
    raise InvalidOverride(f"registration has no rule {rule_id}")
