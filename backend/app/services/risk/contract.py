"""Contract for risk: red-flag rules in code, a weighted score, and a referral decision.

The score and the referral are computed by code. A model writes only the explanation.
Callers import this module only.
"""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError

REFER_AT = 60  # refer to an investigator when the score is at or above this

# Red-flag rule ID -> weight. The score is the sum of the hits' weights, capped at 100.
WEIGHTS = {
    "RF1": 40,  # admitted within 30 days of policy inception
    "RF2": 30,  # claimed amount over half the sum insured
    "RF3": 60,  # a file with the same hash was submitted on another claim
}
EARLY_CLAIM_DAYS = 30
HIGH_CLAIM_SHARE = "0.5"  # of the sum insured


class RiskHit(BaseModel):
    rule_id: str
    weight: int
    detail: str


class RiskReport(BaseModel):
    claim_id: UUID
    registration_version: int  # the RegistrationResult version scored
    hits: list[RiskHit]
    score: int  # 0 to 100
    refer: bool  # score >= REFER_AT
    explanation: str


class NotReadyForRisk(PermanentError):
    """The claim has no registration or no policy yet."""


class RiskService(Protocol):
    async def score(self, claim_id: UUID) -> RiskReport:
        """Idempotent per RegistrationResult version."""
        ...
