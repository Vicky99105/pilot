"""Contract for the rules engine: pure, deterministic checks on claim facts.

Each rule is a pure function (facts, product) -> RuleResult, registered by its ID. A rule
never reads a database or calls a model. Parameters come from the product config in
config/products/. Money is Decimal rupees, quantized to 2 places.

Rules R1 to R26 return a RuleResult each. DRUG-PRICE is a check that never deducts: a failed
result is a finding for the bill processor (pharmacy line above the NPPA ceiling plus GST).

R22 checks the deduction sheet itself: every amount taken off a line names its clause. It
never deducts. R4 and R25 are registration checks; R8 runs at registration and again on the
verified facts after medical review, like R6, R7 and R9.

RuleResult conventions:
- passed=False means an adverse finding: a failed check or a deduction for a breach.
- amount_effect is the change to the payable amount, zero or negative.
- R11 names the list clause whenever it matched a list item, paid or not; a line R11 matched
  no item for has no clause, and only such a line goes to R12.
- A rule that lacks the facts it needs returns passed=False with a reason naming them, so a
  person sees the gap at the checkpoint.
- Package pricing (R20, R23, R24) applies only on the claim routes the product config lists
  in packages.pricing_applies_to. On another route those rules pass and the room and ICU caps
  (R13, R14) and the proportionate deduction (R15) apply to every line, whether or not a
  package was mapped.

Callers import this module only.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, model_validator

from app.integrations.core.contract import InsuredMember, MemberPolicy
from app.shared.errors import PermanentError
from app.shared.types import ClaimRoute, RuleResult

__all__ = ["ClaimRoute"]


class Head(StrEnum):
    """Standard bill heads. Bill review maps every bill line to one."""

    ROOM = "room"
    ICU = "icu"
    NURSING = "nursing"
    OT = "ot"
    PROFESSIONAL_FEES = "professional_fees"
    PHARMACY = "pharmacy"
    CONSUMABLES = "consumables"
    IMPLANTS = "implants"
    DIAGNOSTICS = "diagnostics"
    AMBULANCE = "ambulance"
    PACKAGE = "package"  # a package charge the hospital billed as one line
    OTHER = "other"


# --- Product config -----------------------------------------------------------------------


class DailyCap(BaseModel):
    percent_of_sum_insured: Decimal
    max_per_day: Decimal


class ProportionateDeduction(BaseModel):
    linked_heads: list[Head]
    excluded_heads: list[Head]
    only_with_differential_billing: bool = True

    @model_validator(mode="after")
    def _disjoint(self) -> "ProportionateDeduction":
        both = set(self.linked_heads) & set(self.excluded_heads)
        if both or {Head.ROOM, Head.ICU} & set(self.linked_heads):
            raise ValueError(f"heads both linked and excluded, or room/ICU linked: {both}")
        return self


class CoPay(BaseModel):
    percent: Decimal


class HospitalRules(BaseModel):
    """R4: the claim routes that need a network hospital. Every route needs a hospital the
    hospital register holds."""

    network_required_for: list[ClaimRoute]


class Kyc(BaseModel):
    """R25: from this claimed amount, the claim needs PAN or Aadhaar on file."""

    threshold: Decimal


class InitialWaiting(BaseModel):
    days: int
    accident_exempt: bool = True
    waived_after_continuous_cover_months: int


class GracePeriodDays(BaseModel):
    monthly: int
    other: int


class CataractLimit(BaseModel):
    percent_of_sum_insured: Decimal
    max_per_eye: Decimal


class SubLimits(BaseModel):
    cataract: CataractLimit
    modern_treatments_percent_of_sum_insured: Decimal
    ambulance_max_per_hospitalisation: Decimal


class WaitingCondition(BaseModel):
    """One specified condition with its waiting period and ICD ranges. A condition with no
    ICD-10-CM range says why in `unmapped`."""

    item: str  # as the wording lists it
    months: int
    icd10cm: list[str] = []
    icd10pcs: list[str] = []  # PCS code prefixes, for procedures
    unmapped: str | None = None

    @model_validator(mode="after")
    def _mapped_or_explained(self) -> "WaitingCondition":
        if not self.icd10cm and not self.unmapped:
            raise ValueError(f"{self.item!r}: no ICD-10-CM range and no reason it is unmapped")
        return self


class SpecificWaiting(BaseModel):
    clause: str
    accident_exempt: bool = True
    conditions: list[WaitingCondition]


class ExclusionCodes(BaseModel):
    """A standard exclusion that verified codes can show, with its ICD ranges. An exclusion
    that needs judgement beyond the code is left out of the product config."""

    code: str  # Excl04 to Excl18; also the clause cited
    title: str
    icd10cm: list[str] = []
    icd10pcs: list[str] = []  # PCS code prefixes

    @model_validator(mode="after")
    def _mapped(self) -> "ExclusionCodes":
        if not self.icd10cm and not self.icd10pcs:
            raise ValueError(f"{self.code}: no ICD-10-CM range and no PCS prefix")
        return self


class ModernTreatment(BaseModel):
    item: str  # as the wording lists it
    icd10pcs: list[str] = []  # PCS code prefixes
    unmapped: str | None = None

    @model_validator(mode="after")
    def _mapped_or_explained(self) -> "ModernTreatment":
        if not self.icd10pcs and not self.unmapped:
            raise ValueError(f"{self.item!r}: no PCS prefix and no reason it is unmapped")
        return self


class MultiProcedure(BaseModel):
    """Percent of each package rate paid when one admission has several procedures."""

    first: Decimal
    second: Decimal
    rest: Decimal
    addon: Decimal


class Packages(BaseModel):
    """How the product pays a hospital's package (R20, R23, R24, R13 and R14)."""

    # The claim routes the hospital agreement's pricing binds: the package or tariff cap
    # (R20, with the multi-procedure rule), unbundling (R23) and the non-package discount
    # (R24). On any other route the policy's own limits apply (R13, R14), even when a package
    # is mapped. A wording that caps reimbursement at the network rate lists both routes.
    pricing_applies_to: list[ClaimRoute]
    included_heads: list[Head]  # what a package price covers when the bill is itemised
    multi_procedure_percent: MultiProcedure
    # R24: the agreed discount on non-package items does not apply to these heads.
    discount_excluded_heads: list[Head]


class NonPayableJudging(BaseModel):
    min_confidence: float  # R12: below this, a person decides the line


class DrugPriceCheck(BaseModel):
    gst_percent: Decimal  # NPPA ceiling prices exclude GST; the bill includes it


class Clauses(BaseModel):
    premium_received: str
    policy_period: str
    grace_period: str
    insured_person: str
    initial_waiting: str
    sum_insured: str
    room_cap: str
    icu_cap: str
    proportionate_deduction: str
    co_pay: str
    specific_waiting: str | None = None
    pre_existing: str  # R6
    cataract: str  # R16
    modern_treatments: str  # R17
    pre_post: str  # R18
    ambulance: str  # R19
    package_tariff: str  # R20
    package_inclusions: str  # R23
    non_package_discount: str  # R24
    preauth: str  # R26
    hospital: str  # R4
    moratorium: str  # R8
    deduction_reasons: str  # R22
    kyc: str  # R25
    non_payable: dict[str, str]  # IRDAI list number ("I" to "IV") -> clause


class ProductConfig(BaseModel):
    product_uin: str
    name: str
    version: int
    room_rent: DailyCap
    icu: DailyCap
    proportionate_deduction: ProportionateDeduction
    co_pay: CoPay
    initial_waiting: InitialWaiting
    grace_period_days: GracePeriodDays
    clauses: Clauses
    wording_uin: str | None = None  # the filed wording the limits are checked against
    sub_limits: SubLimits | None = None
    pre_hospitalisation_days: int | None = None
    post_hospitalisation_days: int | None = None
    pre_existing_waiting_months: int | None = None
    moratorium_months: int | None = None  # R8; None: the product has no moratorium
    hospital: HospitalRules
    kyc: Kyc
    specific_waiting: SpecificWaiting | None = None
    packages: Packages
    non_payable_judging: NonPayableJudging
    drug_price: DrugPriceCheck
    cataract_pcs: list[str]  # PCS prefixes for cataract surgery; the 4th character is the eye
    modern_treatments: list[ModernTreatment]
    exclusions: list[ExclusionCodes]


# --- Facts --------------------------------------------------------------------------------


class Claimant(BaseModel):
    """Who the claim says the patient is, as read from the documents."""

    member_id: str | None = None
    name: str | None = None
    dob: date | None = None


class LineFacts(BaseModel):
    line_id: int
    head: Head
    description: str
    qty: Decimal | None = None  # days, for room and ICU lines
    rate: Decimal | None = None  # per day, for room and ICU lines
    claimed: Decimal  # the amount under review: what the bill claims, less earlier rules
    service_date: date | None = None


class RoomFacts(BaseModel):
    sum_insured: Decimal
    room_rate_per_day: Decimal | None  # the room rate charged; None when no room line
    differential_billing: bool  # False when the hospital charges one rate for every room


PayableInProduct = Literal["not_payable", "not_payable_unless", "payable_within_limits", "payable"]
# Flags under which R11 leaves a matching line to be paid (a limit, if any, is another rule's).
PAID_FLAGS = ("payable_within_limits", "payable")


class NonPayableItem(BaseModel):
    list_name: str  # IRDAI list, "I" to "IV"
    item_number: int
    item: str
    condition: str | None = None
    # How the product treats the item: not_payable_unless means a bill line naming only the
    # item does not show the exception, so it is deducted.
    payable_in_product: PayableInProduct = "not_payable"


class MappedPackage(BaseModel):
    """A package the claim's procedure maps to, priced for the hospital's agreement."""

    scheme: str
    package_code: str
    name: str
    rate: Decimal  # the agreed rate (tier, accreditation, ward), before the multi-procedure rule
    addon: bool = False
    standalone: bool = False


class PackageFacts(BaseModel):
    packages: list[MappedPackage]  # one per mapped procedure
    billed_as_package: bool  # the bill has a package charge line
    # R20: what the bill charges for the package: the package lines, or, on an itemised
    # bill, the lines whose head a package covers. Amounts after the earlier rules.
    package_total: Decimal = Decimal("0")


class DrugFacts(BaseModel):
    """The one NPPA formulation that fits a pharmacy line."""

    medicine: str
    dosage_form: str  # e.g. "Injection 500mg/100mL"
    unit: str  # the unit the ceiling is for, e.g. "1 Vial", "1 ML"
    ceiling_price: Decimal  # rupees per unit, before GST


class NonPayableJudgement(BaseModel):
    """R12: the model's reading of a line that may be a non-payable item."""

    item: NonPayableItem | None  # None: the line is payable
    confidence: float


class HospitalFacts(BaseModel):
    """R4: the treating hospital as the hospital register holds it."""

    rohini_id: str | None  # None: no hospital in the register matched the claim
    network: bool = False


class DeductionCitation(BaseModel):
    """R22: one amount taken off a bill line, and the clause it cites."""

    line_id: int | None
    rule_id: str | None  # None for a person's edit
    amount: Decimal
    clause_ref: str | None
    judged_by: Literal["code", "model", "person"] = "code"


class PreAuthFacts(BaseModel):
    approved_total: Decimal  # the pre-auth plus every enhancement
    diagnosis_codes: list[str] = []  # as approved


class Facts(BaseModel):
    """Everything any rule may read. Each rule uses the fields it needs."""

    admission_date: date | None = None
    accident: bool = False
    policy: MemberPolicy | None = None
    claimant: Claimant | None = None
    amount: Decimal | None = None  # the amount to block (R10)
    line: LineFacts | None = None  # the bill line under review (R11 to R15, R18, R23, R24)
    room: RoomFacts | None = None  # R13, R14, R15
    non_payable: list[NonPayableItem] = []  # R11
    admissible_total: Decimal | None = None  # before co-pay (R16, R17, R21, R26)
    sum_insured: Decimal | None = None  # R16, R17
    discharge_date: date | None = None  # R18
    diagnosis_codes: list[str] = []  # verified ICD-10-CM codes (R6, R7, R8, R9, R26)
    procedure_codes: list[str] = []  # verified ICD-10-PCS codes (R7, R9, R16, R17)
    pre_existing_onsets: list[date] = []  # R6, R8: latest start dates of conditions found
    judgement: NonPayableJudgement | None = None  # R12
    package: PackageFacts | None = None  # R13, R14, R15, R20, R23, R24
    # R4: does the route need a network hospital; R13 to R15, R20, R23, R24: does the
    # hospital agreement bind?
    claim_route: ClaimRoute | None = None
    non_package_discount_percent: Decimal | None = None  # R24, from the hospital agreement
    ambulance_total: Decimal | None = None  # R19: ambulance lines after the earlier rules
    preauth: PreAuthFacts | None = None  # R26; None for a reimbursement with no pre-auth
    drug: DrugFacts | None = None  # DRUG-PRICE
    hospital: HospitalFacts | None = None  # R4
    claimed: Decimal | None = None  # R25: the amount the claim asks for
    kyc_on_file: bool | None = None  # R25: the claim holds a PAN or Aadhaar document
    deductions: list[DeductionCitation] | None = None  # R22: every amount taken off a line


MatchKind = Literal["exact_id", "name_dob", "doubtful", "none"]


class MemberMatch(BaseModel):
    kind: MatchKind
    member: InsuredMember | None  # set for exact_id and name_dob only
    score: float  # name similarity, 0 to 100; 100 for an exact ID
    reason: str


class UnknownRule(PermanentError):
    pass


class UnknownProduct(PermanentError):
    pass


class RulesEngine(Protocol):
    def evaluate(
        self, rule_ids: list[str], facts: Facts, product: ProductConfig
    ) -> list[RuleResult]:
        """One result per rule ID, in the order given."""
        ...

    def match_member(self, claimant: Claimant, members: list[InsuredMember]) -> MemberMatch:
        """The insured member the claimant is: exact ID, else name and DOB. R3 uses it."""
        ...

    def product(self, product_uin: str) -> ProductConfig:
        """The product config for a policy's UIN."""
        ...

    def allocate(self, amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
        """Spread a claim-level amount (R16, R17, R19, R20) over lines in proportion to the
        weights (their admissible amounts). Each share is rounded half up to the paisa; the
        last line with weight takes the remainder, so the shares sum to the amount exactly."""
        ...
