"""Contract for bill review: package first. Map the claim's verified procedures to a package
of the hospital's agreement, map each bill line to a standard head, apply the bill rules and
produce the deduction sheet.

Bill review runs after medical coding (MedicalCoding) and before the full medical review,
which reads the package from the sheet.

Order, each rule on what the earlier ones left admissible:
1. Per line: R11 (near-exact non-payable match), then R12 (the model's reading of a line
   that may be a non-payable item, one call for every unclear line), then R18 (pre and post
   hospitalisation windows).
2. Package: the model picks one package per procedure from an enum of the candidates in
   the agreement's scheme, or none. R23 deducts lines a package charge already covers; R20
   caps the package charges at the package total (multi-procedure rule), spread over the
   lines in proportion.
3. Per line outside the package: R24 (agreed non-package discount, never on medicines or
   implants), R13 (room cap and proportionate deduction), R14 (ICU cap).
4. Claim: R19 (ambulance), R16 (cataract), R17 (modern treatments), R26 (pre-auth), each
   spread over the lines; then R21 (co-pay) on the admissible total.

Checks that never deduct are findings: a pharmacy line above the NPPA ceiling plus GST,
a diagnosis the package's crosswalk does not list, or no candidate package. Every amount is
Decimal rupees, quantized to 2 places.

Callers import this module only.
"""

from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.services.knowledge_base.contract import HospitalAgreement
from app.services.rules.contract import Head, MappedPackage
from app.shared.errors import PermanentError
from app.shared.types import RuleResult


class LineDeduction(BaseModel):
    """One rule's deduction on a line."""

    rule_id: str
    amount: Decimal  # positive: the amount this rule took off the line
    clause_ref: str | None
    reason: str
    judged_by: Literal["code", "model", "person"] = "code"


class DeductionLine(BaseModel):
    line_id: int  # the bill line's ID in the ClaimRecord
    head: Head
    head_as_written: str | None  # the bill's section heading
    description: str
    claimed: Decimal
    admissible: Decimal
    deducted: Decimal  # claimed - admissible
    # The rule that took the most off the line, with its clause and reason; every rule that
    # deducted is in deductions, in order.
    rule_id: str | None
    clause_ref: str | None
    reason: str | None
    judged_by: Literal["code", "model", "person"] = "code"
    flagged: bool = False  # True when a person should check the line: no known head, or R12
    # could not decide it
    flag_reason: str | None = None
    in_package: bool = False  # True when a package prices the line
    deductions: list[LineDeduction] = []


class PackageOption(BaseModel):
    """A candidate package offered to the model, priced for the hospital's agreement."""

    scheme: str
    package_code: str
    procedure_code: str
    name: str
    rate: Decimal | None  # None when the agreement's accreditation or ward has no rate
    rate_basis: str | None
    addon: bool = False
    standalone: bool = False
    score: float


class PackageChoice(BaseModel):
    procedure: str  # the procedure as coded: phrase and PCS code
    chosen: str  # a candidate package code, or NO_PACKAGE


class PackageMapping(BaseModel):
    candidates: list[PackageOption]  # the enum the model chose from (plus NO_PACKAGE)
    choices: list[PackageChoice]
    packages: list[MappedPackage]  # the priced packages the rules apply
    billed_as_package: bool  # the bill has a package charge line


BillFindingKind = Literal["drug_price", "package", "diagnosis_package"]


class BillFinding(BaseModel):
    """A check that does not deduct, for the bill processor and the medical officer."""

    kind: BillFindingKind
    text: str
    line_id: int | None = None


NO_PACKAGE = "none"


class DeductionSheet(BaseModel):
    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version the sheet was built from
    registration_version: int  # the RegistrationResult version it used
    coding_version: int = 0  # the MedicalCoding version whose procedures were mapped
    agreement: HospitalAgreement | None = None
    package: PackageMapping | None = None  # None when no package applies
    product_uin: str
    lines: list[DeductionLine]
    rule_results: list[RuleResult]  # every rule result, per line and for the claim
    claimed_total: Decimal
    admissible_total: Decimal  # before co-pay
    co_pay: Decimal  # positive: the amount the insured person bears
    payable: Decimal  # admissible_total - co_pay
    flagged_lines: list[int]  # line IDs mapped to "other" for want of a known head
    findings: list[BillFinding] = []


class LineEdit(BaseModel):
    """A person's change to one line at "confirm deductions"."""

    line_id: int
    admissible: Decimal
    reason: str

    @field_validator("admissible")
    @classmethod
    def _rupees(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class NotReadyForBillReview(PermanentError):
    """The claim has no extracted record, no registration or no medical coding yet."""


class InvalidEdit(PermanentError):
    """An edit names an unknown line, has no reason, or sets an amount outside 0..claimed."""


class BillReviewService(Protocol):
    async def review(self, claim_id: UUID) -> DeductionSheet:
        """Idempotent per (ClaimRecord, RegistrationResult, MedicalCoding) version."""
        ...

    async def apply_edits(
        self, claim_id: UUID, edits: list[LineEdit], *, actor: str
    ) -> DeductionSheet:
        """Store a person's line changes as a new version: judged_by="person", the
        co-pay and payable recomputed."""
        ...
