from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.integrations.core.contract import (
    BlockRef,
    InsuredMember,
    MemberPolicy,
    Payee,
    PaymentRef,
    PremiumPayment,
)


def member_policy(policy_number: str = "P-1", member_id: str = "M-1") -> MemberPolicy:
    return MemberPolicy(
        policy_number=policy_number,
        product_uin="UIN",
        sum_insured=Decimal("300000.00"),
        cumulative_bonus=Decimal("0.00"),
        utilised=Decimal("0.00"),
        inception_date=date(2024, 1, 1),
        continuous_cover_start=date(2024, 1, 1),
        policy_start=date(2026, 1, 1),
        policy_end=date(2026, 12, 31),
        premium_payments=[
            PremiumPayment(paid_on=date(2025, 12, 28), amount=Decimal("9000"), status="received")
        ],
        declared_peds=[],
        insured_members=[
            InsuredMember(
                member_id=member_id, name="A", dob=date(1990, 1, 1), gender="F", relationship="self"
            )
        ],
    )


class FakePolicyService:
    def __init__(self, policies: list[MemberPolicy]) -> None:
        self.policies = policies

    async def find_member(
        self, *, policy_number: str | None, member_id: str | None
    ) -> MemberPolicy | None:
        for p in self.policies:
            members = {m.member_id for m in p.insured_members}
            if (policy_number is None or p.policy_number == policy_number) and (
                member_id is None or member_id in members
            ):
                if policy_number or member_id:
                    return p
        return None


class FakeMoneyService:
    """Records calls; returns the first result for a repeated idempotency key."""

    def __init__(self) -> None:
        self.blocks: dict[str, BlockRef] = {}
        self.releases: dict[str, tuple[str, Decimal]] = {}
        self.adjustments: dict[str, BlockRef] = {}
        self.payments: dict[str, PaymentRef] = {}

    async def block_sum_insured(
        self, *, policy_number: str, member_id: str, amount: Decimal, idempotency_key: str
    ) -> BlockRef:
        if idempotency_key not in self.blocks:
            self.blocks[idempotency_key] = BlockRef(
                block_id=str(uuid4()), amount=amount, available_after=Decimal("0")
            )
        return self.blocks[idempotency_key]

    async def adjust_block(
        self, *, block_id: str, amount: Decimal, idempotency_key: str
    ) -> BlockRef:
        if idempotency_key not in self.adjustments:
            self.adjustments[idempotency_key] = BlockRef(
                block_id=block_id, amount=amount, available_after=Decimal("0")
            )
        return self.adjustments[idempotency_key]

    async def release_block(
        self, *, block_id: str, utilised: Decimal, idempotency_key: str
    ) -> None:
        self.releases.setdefault(idempotency_key, (block_id, utilised))

    async def create_payment_instruction(
        self, *, payee: Payee, amount: Decimal, claim_number: str, idempotency_key: str
    ) -> PaymentRef:
        if idempotency_key not in self.payments:
            self.payments[idempotency_key] = PaymentRef(
                payment_id=str(uuid4()), amount=amount, status="instructed"
            )
        return self.payments[idempotency_key]


class FakePermissionService:
    def __init__(
        self,
        policies: dict[UUID, set[str]] | None = None,
        hospitals: dict[UUID, set[str]] | None = None,
    ) -> None:
        self.policies = policies or {}
        self.hospitals = hospitals or {}

    async def can_submit_for(
        self, *, user_id: UUID, policy_number: str | None, hospital_id: str | None
    ) -> bool:
        return (
            policy_number is not None and policy_number in self.policies.get(user_id, set())
        ) or (hospital_id is not None and hospital_id in self.hospitals.get(user_id, set()))
