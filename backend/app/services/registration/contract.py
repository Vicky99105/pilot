"""Contract for registration: identify the member, policy and hospital, run the eligibility
rules, check for a duplicate claim, block the sum insured and assign the claim number.

Callers import this module only.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.integrations.core.contract import BlockRef, InsuredMember
from app.shared.errors import PermanentError
from app.shared.types import RuleResult

# Rules registration runs, in this order.
REGISTRATION_RULES = ["R3", "R1", "R2", "R4", "R5", "R8", "R10", "R25"]

MemberMatchKind = Literal["exact_id", "name_dob", "doubtful", "none", "person"]
HospitalMatchKind = Literal["rohini_id", "name_city", "none", "person"]


class MatchedMember(BaseModel):
    match: MemberMatchKind
    member_id: str | None  # None when no member could be identified
    name: str | None
    dob: date | None
    relationship: str | None
    reason: str


class PolicySummary(BaseModel):
    policy_number: str
    product_uin: str
    sum_insured: Decimal
    cumulative_bonus: Decimal
    utilised: Decimal
    blocked: Decimal  # blocks held before this claim's block
    balance: Decimal  # available before this claim's block
    policy_start: date
    policy_end: date
    insured_members: list[InsuredMember]


class MatchedHospital(BaseModel):
    match: HospitalMatchKind
    rohini_id: str | None
    name: str | None
    city: str | None
    differential_billing: bool  # True when unknown: the conservative reading
    network: bool = False  # in the insurer's network (R4)


class DuplicateClaim(BaseModel):
    claim_id: UUID
    claim_number: str | None
    admission_date: date
    discharge_date: date | None


class RegistrationResult(BaseModel):
    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version this result was built from
    claim_number: str
    member: MatchedMember
    policy: PolicySummary | None  # None when no policy was found
    hospital: MatchedHospital
    admission_date: date | None
    accident: bool
    block_amount: Decimal | None  # the claimed amount, less what earlier settlements paid
    claimed_amount: Decimal | None = None  # what the claim asks for, in full
    rule_results: list[RuleResult]
    duplicates: list[DuplicateClaim]
    block_ref: BlockRef | None  # None when nothing could be blocked
    overrides: list[str] = []  # what people changed, one line each, with their reasons


class RegistrationOverrides(BaseModel):
    """A person's corrections at "confirm registration"."""

    member_id: str | None = None  # the insured member the patient is
    hospital_id: str | None = None  # the ROHINI ID of the treating hospital
    rules: dict[str, str] = {}  # rule ID -> why a person accepts it despite the code
    reason: str = ""

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class NotDigitised(PermanentError):
    """The claim has no extracted ClaimRecord to register."""


class InvalidOverride(PermanentError):
    """An override names an unknown member, hospital or rule, or gives no reason."""


class RegistrationService(Protocol):
    async def register(self, claim_id: UUID) -> RegistrationResult:
        """Idempotent per ClaimRecord version: a repeat returns the stored result, and the
        sum insured block uses a key derived from the claim and record version."""
        ...

    async def apply_overrides(
        self, claim_id: UUID, overrides: RegistrationOverrides, *, actor: str
    ) -> RegistrationResult:
        """Store a person's corrections as a new version, judged_by="person"."""
        ...
