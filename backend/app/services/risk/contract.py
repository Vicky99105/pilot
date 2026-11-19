"""Contract for risk: red-flag rules in code, a weighted score, an outlier score and a
referral decision.

Every flag, the score, the outlier score and the referral are computed by code, with weights
and thresholds from config/risk.yaml. A model writes only the explanation, from the hits.
Callers import this module only.

Red flags:
- RF1 admitted soon after policy inception; RF4 admitted close to the policy's expiry.
- RF2 claimed amount over a share of the sum insured.
- RF3 a file with the same hash was submitted on another claim.
- RF5 the same diagnosis (3-character ICD-10-CM category) on several recent claims at one
  hospital, or from one doctor.
- RF6 the stay overlaps another claim's stay for the same member.
- RF7 a sudden jump in the hospital's claim volume.
- RF8 room charged above the regional norm for the hospital's city tier.
- RF9 the final bill is not on the hospital's letterhead, or has handwritten values.
- RF10 the member ID, date of birth (age) or gender on the claim differs from the insured
  member registration matched.
- RF11 the bill is far above the mapped package rate; RF12 the stay is far beyond the
  length-of-stay norm. Both also feed the outlier score.

A claim is referred when its score reaches refer_at, or when its claimed amount reaches the
value threshold whatever its score.
"""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

import yaml
from pydantic import BaseModel

from app.shared.errors import PermanentError

FRAUD_CLAUSE = "FRAUD"  # the policy clause a reject for confirmed fraud cites

Verdict = Literal["cleared", "fraud_confirmed"]
ReferReason = Literal["score", "value"]

# Each flag's title, as the explanation and the Review area show it.
FLAG_TITLES = {
    "RF1": "Claim soon after the policy started",
    "RF2": "High claim against the sum insured",
    "RF3": "File reused from another claim",
    "RF4": "Claim close to the policy's expiry",
    "RF5": "Same diagnosis repeated at one hospital or by one doctor",
    "RF6": "Stay overlaps another claim",
    "RF7": "Sudden jump in the hospital's claim volume",
    "RF8": "Room above the regional norm",
    "RF9": "Bill not on letterhead or handwritten",
    "RF10": "ID, age or gender mismatch",
    "RF11": "Bill far above the package rate",
    "RF12": "Stay far beyond the norm",
}


class RepeatedDiagnosis(BaseModel):
    lookback_days: int
    min_other_claims: int


class VolumeJump(BaseModel):
    recent_days: int
    baseline_days: int
    ratio: Decimal
    min_recent: int


class Outlier(BaseModel):
    tariff_ratio: Decimal
    los_extra_days: int
    tariff_points_per_10_percent: int
    los_points_per_day: int


class RiskConfig(BaseModel):
    version: int
    refer_at: int
    investigate_from_amount: Decimal
    weights: dict[str, int]
    near_start_days: int
    near_expiry_days: int
    high_claim_share: Decimal
    repeated_diagnosis: RepeatedDiagnosis
    volume_jump: VolumeJump
    room_norm_per_day: dict[str, Decimal]  # CGHS city tier -> rupees a day
    outlier: Outlier


def load_risk_config(path: Path) -> RiskConfig:
    return RiskConfig.model_validate(yaml.safe_load(path.read_text()))


class RiskHit(BaseModel):
    rule_id: str
    weight: int
    detail: str
    title: str = ""


class OutlierScore(BaseModel):
    """The claim against its tariff and length-of-stay baselines."""

    score: int  # 0 to 100
    tariff_ratio: Decimal | None  # bill total / package rate; None without a priced package
    package_rate: Decimal | None
    stay_days: int | None
    los_norm_days: int | None


class RiskReport(BaseModel):
    claim_id: UUID
    registration_version: int  # the RegistrationResult version scored
    bill_review_version: int | None = None  # the DeductionSheet version read, if any
    medical_review_version: int | None = None  # the MedicalFindings version read, if any
    config_version: int | None = None
    hits: list[RiskHit]
    score: int  # 0 to 100
    outlier: OutlierScore | None = None
    refer: bool  # score >= refer_at, or the claimed amount at or above the value threshold
    refer_reasons: list[ReferReason] = []
    explanation: str
    # Set by an investigator on a referred claim. "fraud_confirmed" makes adjudication reject.
    verdict: Verdict | None = None
    verdict_note: str | None = None
    verdict_by: str | None = None


class NotReadyForRisk(PermanentError):
    """The claim has no registration or no policy yet."""


class InvalidVerdict(PermanentError):
    """The verdict has no note, or the claim has no risk report to rule on."""


class RiskService(Protocol):
    async def score(self, claim_id: UUID) -> RiskReport:
        """Idempotent per (RegistrationResult, DeductionSheet, MedicalFindings) version."""
        ...

    async def record_verdict(
        self, claim_id: UUID, verdict: Verdict, note: str, *, actor: str
    ) -> RiskReport:
        """An investigator's verdict on the latest report, written as a new RiskReport
        version. The score and the hits are unchanged."""
        ...
