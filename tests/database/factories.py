"""Row builders for DB-backed tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, UserRoleRow, UserRow
from app.shared.types import RequestType, Role


async def add_claim(sessions: async_sessionmaker[AsyncSession], status: str = "intake") -> UUID:
    claim = ClaimRow(
        id=uuid4(),
        request_type=RequestType.REIMBURSEMENT.value,
        status=status,
        status_changed_at=datetime.now(UTC),
    )
    async with sessions.begin() as session:
        session.add(claim)
    return claim.id


async def add_user(
    sessions: async_sessionmaker[AsyncSession],
    name: str,
    roles: list[Role],
    max_amount: Decimal | None = None,
    system_of_medicine: str | None = None,
) -> UUID:
    """max_amount is the limit on the user's APPROVER role, if they hold it."""
    user = UserRow(id=uuid4(), name=name, kind="staff", system_of_medicine=system_of_medicine)
    async with sessions.begin() as session:
        session.add(user)
        await session.flush()
        session.add_all(
            [
                UserRoleRow(
                    user_id=user.id,
                    role=r.value,
                    max_amount=max_amount if r is Role.APPROVER else None,
                )
                for r in roles
            ]
        )
    return user.id
