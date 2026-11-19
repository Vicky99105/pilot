from decimal import Decimal
from uuid import uuid4

from app.integrations.core.contract import MoneyService, Payee, PermissionService, PolicyService
from tests.fakes.core import (
    FakeMoneyService,
    FakePermissionService,
    FakePolicyService,
    member_policy,
)


async def test_fake_policy_service_finds_by_member_or_policy() -> None:
    policy = member_policy()
    service: PolicyService = FakePolicyService([policy])
    member_id = policy.insured_members[0].member_id

    assert await service.find_member(policy_number=None, member_id=member_id) == policy
    assert await service.find_member(policy_number=policy.policy_number, member_id=None) == policy
    assert await service.find_member(policy_number=None, member_id="x") is None


async def test_fake_money_service_is_idempotent() -> None:
    money: MoneyService = FakeMoneyService()
    payee = Payee(kind="member", name="A", account_number="1", ifsc="MOCK0000001")

    a = await money.block_sum_insured(
        policy_number="P", member_id="M", amount=Decimal("10"), idempotency_key="k"
    )
    b = await money.block_sum_insured(
        policy_number="P", member_id="M", amount=Decimal("10"), idempotency_key="k"
    )
    p1 = await money.create_payment_instruction(
        payee=payee, amount=Decimal("5"), claim_number="C", idempotency_key="p"
    )
    p2 = await money.create_payment_instruction(
        payee=payee, amount=Decimal("5"), claim_number="C", idempotency_key="p"
    )

    assert a == b and p1 == p2


async def test_fake_permission_service_uses_grants() -> None:
    user = uuid4()
    perm: PermissionService = FakePermissionService(policies={user: {"P1"}})

    assert await perm.can_submit_for(user_id=user, policy_number="P1", hospital_id=None)
    assert not await perm.can_submit_for(user_id=user, policy_number="P2", hospital_id=None)
