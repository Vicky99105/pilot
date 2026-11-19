from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.services.users.contract import UserService
from app.services.users.service import DefaultUserService
from app.shared.seed_ids import DOCTOR_USER_ID, MEMBER_POLICY_NUMBER, MEMBER_USER_ID
from app.shared.types import Role


async def test_get_returns_seeded_user_with_roles_and_limit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    users: UserService = DefaultUserService(sessions)

    doctor = await users.get(DOCTOR_USER_ID)
    member = await users.get(MEMBER_USER_ID)

    assert doctor is not None and member is not None
    assert set(doctor.roles) == {Role.MEDICAL_OFFICER, Role.APPROVER, Role.CRC_REVIEWER}
    assert doctor.approval_limit == Decimal("100000.00")
    assert (member.kind, member.policy_number, member.roles) == (
        "member",
        MEMBER_POLICY_NUMBER,
        [Role.MEMBER],
    )


async def test_get_unknown_user_returns_none(sessions: async_sessionmaker[AsyncSession]) -> None:
    assert await DefaultUserService(sessions).get(uuid4()) is None
