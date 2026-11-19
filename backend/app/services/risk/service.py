"""Default risk scoring: three red-flag rules in code, a capped weighted score, and one
FAST_TEXT call that explains the hits in words. The model never sets the score."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import FileRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.integrations.core.contract import PolicyService
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.registration.contract import RegistrationResult
from app.services.risk.contract import (
    EARLY_CLAIM_DAYS,
    HIGH_CLAIM_SHARE,
    REFER_AT,
    WEIGHTS,
    InvalidVerdict,
    NotReadyForRisk,
    RiskHit,
    RiskReport,
    Verdict,
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
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._policies = policies
        self._records = StageRecordRepository(sessions)

    async def score(self, claim_id: UUID) -> RiskReport:
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        if registered is None:
            raise NotReadyForRisk(f"claim {claim_id} is not registered")
        latest = await self._records.latest(claim_id, Stage.RISK)
        if latest is not None and latest.payload["registration_version"] == registered.version:
            return RiskReport.model_validate(latest.payload)

        registration = RegistrationResult.model_validate(registered.payload)
        if registration.policy is None:
            raise NotReadyForRisk(f"claim {claim_id} has no policy")
        policy = await self._policies.find_member(
            policy_number=registration.policy.policy_number,
            member_id=registration.member.member_id,
        )
        hits: list[RiskHit] = []
        admitted = registration.admission_date
        if policy is not None and admitted is not None:
            days = (admitted - policy.inception_date).days
            if days < EARLY_CLAIM_DAYS:
                hits.append(
                    _hit(
                        "RF1",
                        f"admitted {days} days after policy inception on {policy.inception_date}",
                    )
                )
        claimed = registration.block_amount
        sum_insured = registration.policy.sum_insured
        if claimed is not None and claimed > sum_insured * Decimal(HIGH_CLAIM_SHARE):
            hits.append(
                _hit(
                    "RF2",
                    f"claimed Rs {claimed:,.2f} is over half the sum insured Rs {sum_insured:,.2f}",
                )
            )
        reused = await self._reused_files(claim_id)
        if reused:
            hits.append(_hit("RF3", f"files also submitted on claims {', '.join(reused)}"))

        score = min(100, sum(h.weight for h in hits))
        refer = score >= REFER_AT
        report = RiskReport(
            claim_id=claim_id,
            registration_version=registered.version,
            hits=hits,
            score=score,
            refer=refer,
            explanation=await self._explain(claim_id, hits, score, refer),
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

    async def _explain(self, claim_id: UUID, hits: list[RiskHit], score: int, refer: bool) -> str:
        if not hits:
            return NO_FLAGS
        reply = await self._gateway.structured(
            ModelRole.FAST_TEXT,
            risk_messages([h.model_dump() for h in hits], score, refer),
            RiskExplanation,
            claim_id=claim_id,
            purpose=PURPOSE,
        )
        return reply.text.strip()


def _hit(rule_id: str, detail: str) -> RiskHit:
    return RiskHit(rule_id=rule_id, weight=WEIGHTS[rule_id], detail=detail)
