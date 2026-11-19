"""Contract for the rules engine: pure, deterministic checks on claim facts.

Each rule is a pure function (facts, product) -> RuleResult, registered by its ID. A rule
never reads a database or calls a model. Parameters come from the product config in
config/products/. Money is Decimal rupees, quantized to 2 places.

RuleResult conventions:
- passed=False means an adverse finding: a failed check or a deduction for a breach.
- amount_effect is the change to the payable amount, zero or negative.
- A rule that lacks the facts it needs returns passed=False with a reason naming them, so a
  person sees the gap at the checkpoint.

Callers import this module only.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, model_validator

from app.integrations.core.contract import InsuredMember, MemberPolicy
from app.shared.errors import PermanentError
from app.shared.types import RuleResult


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


class InitialWaiting(BaseModel):
    days: int
    accident_exempt: bool = True
    waived_after_continuous_cover_months: int


class GracePeriodDays(BaseModel):
    monthly: int
    other: int


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
    claimed: Decimal


class RoomFacts(BaseModel):
    sum_insured: Decimal
    room_rate_per_day: Decimal | None  # the room rate charged; None when no room line
    differential_billing: bool  # False when the hospital charges one rate for every room


class NonPayableItem(BaseModel):
    list_name: str  # IRDAI list, "I" to "IV"
    item_number: int
    item: str
    condition: str | None = None


class Facts(BaseModel):
    """Everything any rule may read. Each rule uses the fields it needs."""

    admission_date: date | None = None
    accident: bool = False
    policy: MemberPolicy | None = None
    claimant: Claimant | None = None
    amount: Decimal | None = None  # the amount to block (R10)
    line: LineFacts | None = None  # the bill line under review (R11, R13)
    room: RoomFacts | None = None  # R13
    non_payable: list[NonPayableItem] = []  # R11
    admissible_total: Decimal | None = None  # before co-pay (R21)


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
