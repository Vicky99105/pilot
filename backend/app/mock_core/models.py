"""Tables of the mock core system. They live in the separate mock_core database."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Date, ForeignKey, Numeric, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.database.models import UTCDateTime, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(14, 2)


class MockCoreBase(DeclarativeBase):
    pass


class McPolicy(MockCoreBase):
    __tablename__ = "mc_policies"

    policy_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    product_uin: Mapped[str] = mapped_column(String(60))
    sum_insured: Mapped[Decimal] = mapped_column(Money)
    cumulative_bonus: Mapped[Decimal] = mapped_column(Money)
    utilised: Mapped[Decimal] = mapped_column(Money)
    blocked: Mapped[Decimal] = mapped_column(Money)
    inception_date: Mapped[date] = mapped_column(Date)
    continuous_cover_start: Mapped[date] = mapped_column(Date)
    policy_start: Mapped[date] = mapped_column(Date)
    policy_end: Mapped[date] = mapped_column(Date)
    declared_peds: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)  # Code dicts


class McInsuredMember(MockCoreBase):
    __tablename__ = "mc_insured_members"

    member_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    policy_number: Mapped[str] = mapped_column(ForeignKey("mc_policies.policy_number"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    dob: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(1))
    relationship: Mapped[str] = mapped_column(String(20))


class McPremiumPayment(MockCoreBase):
    __tablename__ = "mc_premium_payments"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    policy_number: Mapped[str] = mapped_column(ForeignKey("mc_policies.policy_number"), index=True)
    paid_on: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(20))


class McBlock(MockCoreBase):
    __tablename__ = "mc_blocks"

    block_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    policy_number: Mapped[str] = mapped_column(ForeignKey("mc_policies.policy_number"))
    member_id: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(20))  # active, released
    utilised: Mapped[Decimal | None] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class McPayment(MockCoreBase):
    __tablename__ = "mc_payments"

    payment_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_number: Mapped[str] = mapped_column(String(40))
    payee: Mapped[dict[str, Any]] = mapped_column(JsonType)
    amount: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class McSubmitGrant(MockCoreBase):
    __tablename__ = "mc_submit_grants"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    policy_number: Mapped[str | None] = mapped_column(String(50))
    hospital_id: Mapped[str | None] = mapped_column(String(13))


class McIdempotency(MockCoreBase):
    """First response for each idempotency key; a repeat request gets it back unchanged."""

    __tablename__ = "mc_idempotency"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    operation: Mapped[str] = mapped_column(String(40))
    response: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
