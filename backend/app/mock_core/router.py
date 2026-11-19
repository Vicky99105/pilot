"""Mock of the ConsintIQ core systems, served at /mock-core.

It uses its own database (MOCK_CORE_DATABASE_URL). Money endpoints are idempotent: a repeated
idempotency_key returns the first successful response and changes nothing.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.core.contract import (
    BlockRef,
    InsuredMember,
    MemberPolicy,
    Payee,
    PaymentRef,
    PremiumPayment,
)
from app.mock_core.models import (
    McBlock,
    McIdempotency,
    McInsuredMember,
    McPayment,
    McPolicy,
    McPremiumPayment,
    McSubmitGrant,
)
from app.shared.types import Code

router = APIRouter(tags=["mock-core"])
CENT = Decimal("0.01")


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.mock_core_sessions
    return sessions


Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(_sessions)]


class BlockRequest(BaseModel):
    policy_number: str
    member_id: str
    amount: Decimal
    idempotency_key: str


class ReleaseRequest(BaseModel):
    utilised: Decimal
    idempotency_key: str


class PaymentRequest(BaseModel):
    payee: Payee
    amount: Decimal
    claim_number: str
    idempotency_key: str


class PermissionRequest(BaseModel):
    user_id: UUID
    policy_number: str | None = None
    hospital_id: str | None = None


class PermissionAnswer(BaseModel):
    allowed: bool


@router.get("/members", response_model=MemberPolicy)
async def find_member(
    sessions: Sessions, policy_number: str | None = None, member_id: str | None = None
) -> MemberPolicy:
    async with sessions() as session:
        if member_id:
            member = await session.get(McInsuredMember, member_id)
            if member is None or (policy_number and member.policy_number != policy_number):
                raise HTTPException(404, "member not found")
            policy_number = member.policy_number
        policy = await session.get(McPolicy, policy_number) if policy_number else None
        if policy is None:
            raise HTTPException(404, "policy not found")
        members = await session.scalars(
            select(McInsuredMember).where(McInsuredMember.policy_number == policy.policy_number)
        )
        payments = await session.scalars(
            select(McPremiumPayment)
            .where(McPremiumPayment.policy_number == policy.policy_number)
            .order_by(McPremiumPayment.paid_on)
        )
        return MemberPolicy(
            policy_number=policy.policy_number,
            product_uin=policy.product_uin,
            sum_insured=policy.sum_insured,
            cumulative_bonus=policy.cumulative_bonus,
            utilised=policy.utilised,
            inception_date=policy.inception_date,
            continuous_cover_start=policy.continuous_cover_start,
            policy_start=policy.policy_start,
            policy_end=policy.policy_end,
            premium_payments=[
                PremiumPayment(paid_on=p.paid_on, amount=p.amount, status=p.status)
                for p in payments
            ],
            declared_peds=[Code.model_validate(c) for c in policy.declared_peds],
            insured_members=[
                InsuredMember(
                    member_id=m.member_id,
                    name=m.name,
                    dob=m.dob,
                    gender=m.gender,
                    relationship=m.relationship,
                )
                for m in members
            ],
        )


@router.post("/blocks", response_model=BlockRef)
async def block_sum_insured(body: BlockRequest, sessions: Sessions) -> dict[str, Any]:
    async def perform(session: AsyncSession) -> dict[str, Any]:
        policy = await session.get(McPolicy, body.policy_number, with_for_update=True)
        member = await session.get(McInsuredMember, body.member_id)
        if policy is None or member is None or member.policy_number != policy.policy_number:
            raise HTTPException(404, "member not found on policy")
        amount = body.amount.quantize(CENT)
        available = policy.sum_insured + policy.cumulative_bonus - policy.utilised - policy.blocked
        if amount <= 0 or amount > available:
            raise HTTPException(409, f"insufficient sum insured: available {available}")
        policy.blocked += amount
        block = McBlock(
            block_id=uuid4(),
            policy_number=policy.policy_number,
            member_id=member.member_id,
            amount=amount,
            status="active",
        )
        session.add(block)
        ref = BlockRef(
            block_id=str(block.block_id), amount=amount, available_after=available - amount
        )
        return ref.model_dump(mode="json")

    return await _idempotent(sessions, body.idempotency_key, "block", perform)


@router.post("/blocks/{block_id}/release")
async def release_block(block_id: str, body: ReleaseRequest, sessions: Sessions) -> dict[str, Any]:
    async def perform(session: AsyncSession) -> dict[str, Any]:
        try:
            block = await session.get(McBlock, UUID(block_id), with_for_update=True)
        except ValueError:
            block = None
        if block is None:
            raise HTTPException(404, "block not found")
        utilised = body.utilised.quantize(CENT)
        if block.status != "active" or not (0 <= utilised <= block.amount):
            raise HTTPException(409, "block not active or utilised outside 0..amount")
        policy = await session.get(McPolicy, block.policy_number, with_for_update=True)
        assert policy is not None
        policy.blocked -= block.amount
        policy.utilised += utilised
        block.status, block.utilised = "released", utilised
        return {"block_id": block_id, "status": "released"}

    return await _idempotent(sessions, body.idempotency_key, "release", perform)


@router.post("/payments", response_model=PaymentRef)
async def create_payment_instruction(body: PaymentRequest, sessions: Sessions) -> dict[str, Any]:
    async def perform(session: AsyncSession) -> dict[str, Any]:
        amount = body.amount.quantize(CENT)
        if amount <= 0:
            raise HTTPException(422, "amount must be positive")
        payment = McPayment(
            payment_id=uuid4(),
            claim_number=body.claim_number,
            payee=body.payee.model_dump(mode="json"),
            amount=amount,
        )
        session.add(payment)
        ref = PaymentRef(payment_id=str(payment.payment_id), amount=amount, status="instructed")
        return ref.model_dump(mode="json")

    return await _idempotent(sessions, body.idempotency_key, "payment", perform)


@router.post("/permissions/can-submit", response_model=PermissionAnswer)
async def can_submit_for(body: PermissionRequest, sessions: Sessions) -> PermissionAnswer:
    matches = []
    if body.policy_number:
        matches.append(McSubmitGrant.policy_number == body.policy_number)
    if body.hospital_id:
        matches.append(McSubmitGrant.hospital_id == body.hospital_id)
    if not matches:
        return PermissionAnswer(allowed=False)
    async with sessions() as session:
        grant = await session.scalar(
            select(McSubmitGrant.id)
            .where(McSubmitGrant.user_id == body.user_id, or_(*matches))
            .limit(1)
        )
    return PermissionAnswer(allowed=grant is not None)


async def _idempotent(
    sessions: async_sessionmaker[AsyncSession],
    key: str,
    operation: str,
    perform: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run perform and store its response under key, in one transaction. A repeated key
    returns the stored response. Two concurrent first calls race on the primary key; the
    loser's transaction (including its money change) rolls back and it returns the winner's."""
    try:
        async with sessions.begin() as session:
            stored = await session.get(McIdempotency, key)
            if stored is not None:
                return stored.response
            response = await perform(session)
            session.add(McIdempotency(key=key, operation=operation, response=response))
        return response
    except IntegrityError:
        async with sessions() as session:
            stored = await session.get(McIdempotency, key)
        if stored is None:
            raise
        return stored.response
