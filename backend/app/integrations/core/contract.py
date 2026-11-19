"""Contracts for the ConsintIQ core systems (policy, money, permissions).

Phase 1 reaches them over HTTP at the mock_core router. Swapping in the real core API is a
base-URL change. Money is Decimal rupees, quantized to 2 places.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import Code


class PremiumPayment(BaseModel):
    paid_on: date
    amount: Decimal
    status: Literal["received", "pending", "bounced"]


class InsuredMember(BaseModel):
    member_id: str
    name: str
    dob: date
    gender: Literal["M", "F", "O"]
    relationship: str  # self, spouse, son, daughter, father, mother


PremiumFrequency = Literal["monthly", "quarterly", "half_yearly", "annual"]


class MemberPolicy(BaseModel):
    policy_number: str
    product_uin: str
    sum_insured: Decimal
    cumulative_bonus: Decimal
    utilised: Decimal
    blocked: Decimal = Decimal("0")  # active blocks not yet released
    inception_date: date
    continuous_cover_start: date
    policy_start: date
    policy_end: date
    premium_frequency: PremiumFrequency = "annual"
    premium_payments: list[PremiumPayment]
    declared_peds: list[Code]
    insured_members: list[InsuredMember]


class BlockRef(BaseModel):
    block_id: str
    amount: Decimal
    available_after: Decimal


class Payee(BaseModel):
    kind: Literal["member", "hospital"]
    name: str
    account_number: str
    ifsc: str


class PaymentRef(BaseModel):
    payment_id: str
    amount: Decimal
    status: Literal["instructed"]


class CoreUnavailable(Exception):
    """The core system did not answer. A retry can succeed."""


class CoreRequestError(PermanentError):
    """The core system refused the request."""


class InsufficientSumInsured(CoreRequestError):
    pass


class BlockNotFound(CoreRequestError):
    pass


class PolicyService(Protocol):
    async def find_member(
        self, *, policy_number: str | None, member_id: str | None
    ) -> MemberPolicy | None: ...


class MoneyService(Protocol):
    async def block_sum_insured(
        self, *, policy_number: str, member_id: str, amount: Decimal, idempotency_key: str
    ) -> BlockRef: ...

    async def adjust_block(
        self, *, block_id: str, amount: Decimal, idempotency_key: str
    ) -> BlockRef:
        """Set an active block to amount: an enhancement raises it, a pre-auth approved
        below its estimate lowers it. A raise beyond the available sum insured is refused."""
        ...

    async def release_block(
        self, *, block_id: str, utilised: Decimal, idempotency_key: str
    ) -> None: ...

    async def create_payment_instruction(
        self, *, payee: Payee, amount: Decimal, claim_number: str, idempotency_key: str
    ) -> PaymentRef: ...


class PermissionService(Protocol):
    async def can_submit_for(
        self, *, user_id: UUID, policy_number: str | None, hospital_id: str | None
    ) -> bool: ...
