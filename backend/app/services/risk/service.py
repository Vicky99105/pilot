"""Default risk scoring: the red flags in code (_helpers), a capped weighted score, the
outlier score, the referral, and one FAST_TEXT call that explains the hits in words. The
model never sets the score; it sees only the hits."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow, FileRow, SubmissionRow, utcnow
from app.database.repositories.stage_records import StageRecord, StageRecordRepository
from app.integrations.core.contract import PolicyService
from app.services.bill_review.contract import DeductionSheet
from app.services.digitisation.contract import ClaimRecord
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.medical_review.contract import MedicalFindings
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.registration.contract import RegistrationResult
from app.services.risk import _helpers as flags
from app.services.risk.contract import (
    FLAG_TITLES,
    InvalidVerdict,
    NotReadyForRisk,
    ReferReason,
    RiskConfig,
    RiskHit,
    RiskReport,
    Verdict,
    load_risk_config,
)
from app.services.risk.prompts import PURPOSE, RiskExplanation, risk_messages
from app.shared.types import Stage

NO_FLAGS = "No red flags."


class DefaultRiskService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: ModelGateway,
        policies: PolicyService,
        *,
        kb: KnowledgeBase | None = None,
        config: RiskConfig | None = None,
    ) -> None:
        """Without a knowledge base the room norm (RF8) is not checked: the city tier is
        unknown."""
        self._sessions = sessions
        self._gateway = gateway
        self._policies = policies
        self._kb = kb
        self._config = config or load_risk_config(Settings().config_dir / "risk.yaml")
        self._records = StageRecordRepository(sessions)

    async def score(self, claim_id: UUID) -> RiskReport:
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        if registered is None:
            raise NotReadyForRisk(f"claim {claim_id} is not registered")
        reviewed = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        medical = await self._records.latest(claim_id, Stage.MEDICAL_REVIEW)
        versions = (registered.version, _version(reviewed), _version(medical))
        latest = await self._records.latest(claim_id, Stage.RISK)
        if (
            latest is not None
            and (
                latest.payload["registration_version"],
                latest.payload.get("bill_review_version"),
                latest.payload.get("medical_review_version"),
            )
            == versions
        ):
            return RiskReport.model_validate(latest.payload)

        registration = RegistrationResult.model_validate(registered.payload)
        if registration.policy is None:
            raise NotReadyForRisk(f"claim {claim_id} has no policy")
        policy = await self._policies.find_member(
            policy_number=registration.policy.policy_number,
            member_id=registration.member.member_id,
        )
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        record = ClaimRecord.model_validate(digitised.payload) if digitised else None
        sheet = DeductionSheet.model_validate(reviewed.payload) if reviewed else None
        findings = MedicalFindings.model_validate(medical.payload) if medical else None
        config = self._config
        claimed = registration.claimed_amount or registration.block_amount

        found: list[tuple[str, str]] = []
        found += flags.policy_timing(registration, policy, config)
        found += flags.high_claim(claimed, registration, config)
        reused = await self._reused_files(claim_id)
        if reused:
            found.append(("RF3", f"files also submitted on claims {', '.join(reused)}"))
        found += await self._repeated_diagnosis(claim_id, record)
        if registration.duplicates:
            others = ", ".join(d.claim_number or str(d.claim_id) for d in registration.duplicates)
            found.append(("RF6", f"the stay overlaps the stay on claims {others}"))
        found += await self._volume_jump(registration)
        found += await self._room_norm(registration, record, sheet)
        found += flags.bill_form(record)
        found += flags.identity(record, registration)
        outlier, outlier_hits = flags.outlier(record, sheet, findings, claimed, config)
        found += outlier_hits

        hits = [
            RiskHit(
                rule_id=rule_id,
                weight=config.weights[rule_id],
                detail=detail,
                title=FLAG_TITLES[rule_id],
            )
            for rule_id, detail in sorted(found, key=lambda h: int(h[0].removeprefix("RF")))
        ]
        score = min(100, sum(h.weight for h in hits))
        reasons: list[ReferReason] = []
        if score >= config.refer_at:
            reasons.append("score")
        if claimed is not None and claimed >= config.investigate_from_amount:
            reasons.append("value")
        report = RiskReport(
            claim_id=claim_id,
            registration_version=registered.version,
            bill_review_version=versions[1],
            medical_review_version=versions[2],
            config_version=config.version,
            hits=hits,
            score=score,
            outlier=outlier,
            refer=bool(reasons),
            refer_reasons=reasons,
            explanation=await self._explain(claim_id, hits, score, reasons),
        )
        await self._records.append(
            claim_id, Stage.RISK, report.model_dump(mode="json"), created_by="code"
        )
        return report

    async def record_verdict(
        self, claim_id: UUID, verdict: Verdict, note: str, *, actor: str
    ) -> RiskReport:
        note = note.strip()
        if not note:
            raise InvalidVerdict("a verdict needs a note of what the investigator found")
        latest = await self._records.latest(claim_id, Stage.RISK)
        if latest is None:
            raise InvalidVerdict(f"claim {claim_id} has no risk report to rule on")
        report = RiskReport.model_validate(latest.payload).model_copy(
            update={"verdict": verdict, "verdict_note": note, "verdict_by": actor}
        )
        await self._records.append(
            claim_id, Stage.RISK, report.model_dump(mode="json"), created_by=actor
        )
        return report

    # --- Flags that read other claims ----------------------------------------------------

    async def _reused_files(self, claim_id: UUID) -> list[str]:
        """Other claims that hold a file with the same hash as one of this claim's files."""
        async with self._sessions() as session:
            mine = (
                select(FileRow.sha256)
                .join(SubmissionRow, SubmissionRow.id == FileRow.submission_id)
                .where(SubmissionRow.claim_id == claim_id)
            )
            others = await session.scalars(
                select(SubmissionRow.claim_id)
                .join(FileRow, FileRow.submission_id == SubmissionRow.id)
                .where(
                    FileRow.sha256.in_(mine),
                    SubmissionRow.claim_id.is_not(None),
                    SubmissionRow.claim_id != claim_id,
                )
                .distinct()
            )
            return sorted(str(c) for c in others)

    async def _repeated_diagnosis(
        self, claim_id: UUID, record: ClaimRecord | None
    ) -> list[tuple[str, str]]:
        """RF5: other recent claims at the same hospital, or from the same doctor, whose
        verified primary diagnosis has this claim's 3-character category."""
        coded = await self._records.latest(claim_id, Stage.MEDICAL_CODING)
        category = flags.primary_category(coded.payload if coded else None)
        if category is None:
            return []
        rules = self._config.repeated_diagnosis
        since = utcnow() - timedelta(days=rules.lookback_days)
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
            assert claim is not None
            others = list(
                await session.scalars(
                    select(ClaimRow.id).where(ClaimRow.id != claim_id, ClaimRow.created_at >= since)
                )
            )
            hospital_ids = {
                c.id: c.hospital_id
                for c in await session.scalars(select(ClaimRow).where(ClaimRow.id.in_(others)))
            }
        doctor = flags.doctor(record)
        same_hospital, same_doctor = [], []
        for other in others:
            other_coded = await self._records.latest(other, Stage.MEDICAL_CODING)
            if flags.primary_category(other_coded.payload if other_coded else None) != category:
                continue
            if claim.hospital_id and hospital_ids.get(other) == claim.hospital_id:
                same_hospital.append(other)
            elif doctor is not None:
                digitised = await self._records.latest(other, Stage.DIGITISATION)
                theirs = ClaimRecord.model_validate(digitised.payload) if digitised else None
                if flags.doctor(theirs) == doctor:
                    same_doctor.append(other)
        found = []
        if len(same_hospital) >= rules.min_other_claims:
            found.append(
                f"{category} on {len(same_hospital)} other claims at hospital "
                f"{claim.hospital_id} in {rules.lookback_days} days"
            )
        if len(same_doctor) >= rules.min_other_claims:
            found.append(
                f"{category} on {len(same_doctor)} other claims from doctor {doctor} in "
                f"{rules.lookback_days} days"
            )
        return [("RF5", "; ".join(found))] if found else []

    async def _volume_jump(self, registration: RegistrationResult) -> list[tuple[str, str]]:
        """RF7: the hospital's claims in the recent window against its average per window
        over the baseline before it."""
        hospital_id = registration.hospital.rohini_id
        if not hospital_id or registration.hospital.match == "none":
            return []
        rules = self._config.volume_jump
        now = utcnow()
        recent_from = now - timedelta(days=rules.recent_days)
        baseline_from = recent_from - timedelta(days=rules.baseline_days)

        async def count(start: datetime, end: datetime) -> int:
            async with self._sessions() as session:
                found = await session.scalar(
                    select(func.count(ClaimRow.id)).where(
                        ClaimRow.hospital_id == hospital_id,
                        ClaimRow.created_at >= start,
                        ClaimRow.created_at < end,
                    )
                )
            return int(found or 0)

        recent = await count(recent_from, now + timedelta(seconds=1))
        baseline = await count(baseline_from, recent_from)
        hit = flags.volume_jump(recent, baseline, rules)
        return [("RF7", f"hospital {hospital_id}: {hit}")] if hit else []

    async def _room_norm(
        self,
        registration: RegistrationResult,
        record: ClaimRecord | None,
        sheet: DeductionSheet | None,
    ) -> list[tuple[str, str]]:
        """RF8: the room rate a day against the norm for the hospital's city tier."""
        hospital_id = registration.hospital.rohini_id
        if self._kb is None or not hospital_id or registration.hospital.match == "none":
            return []
        rate = flags.room_rate(record, sheet)
        if rate is None:
            return []
        tier = await self._kb.city_tier(hospital_id)
        norm = self._config.room_norm_per_day.get(tier) if tier else None
        if norm is None or rate <= norm:
            return []
        return [
            (
                "RF8",
                f"room Rs {rate:,.2f} a day against the Rs {norm:,.2f} norm for a {tier}-tier "
                f"city ({registration.hospital.city})",
            )
        ]

    async def _explain(
        self, claim_id: UUID, hits: list[RiskHit], score: int, reasons: list[ReferReason]
    ) -> str:
        if not hits and not reasons:
            return NO_FLAGS
        reply = await self._gateway.structured(
            ModelRole.FAST_TEXT,
            risk_messages(hits, score, reasons),
            RiskExplanation,
            claim_id=claim_id,
            purpose=PURPOSE,
        )
        return reply.text.strip()


def _version(record: StageRecord | None) -> int | None:
    return record.version if record else None
