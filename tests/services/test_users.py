from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.services.users.contract import UserService
from app.services.users.service import DefaultUserService
from app.shared.seed_ids import (
    AYUSH_DOCTOR_USER_ID,
    DOCTOR_USER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    SENIOR_USER_ID,
    SUPERVISOR_USER_ID,
)
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
    assert doctor.max_amount == Decimal("100000.00")
    assert (member.kind, member.policy_number, member.roles) == (
        "member",
        MEMBER_POLICY_NUMBER,
        [Role.MEMBER],
    )


async def test_get_unknown_user_returns_none(sessions: async_sessionmaker[AsyncSession]) -> None:
    assert await DefaultUserService(sessions).get(uuid4()) is None


async def test_the_senior_approver_has_no_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_claims_db(sessions)

    senior = await DefaultUserService(sessions).get(SENIOR_USER_ID)

    assert senior is not None
    assert (senior.roles, senior.max_amount) == ([Role.APPROVER], None)


async def test_the_seed_has_a_supervisor_and_medical_officers_of_both_systems(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    users = DefaultUserService(sessions)

    supervisor = await users.get(SUPERVISOR_USER_ID)
    doctor = await users.get(DOCTOR_USER_ID)
    vaidya = await users.get(AYUSH_DOCTOR_USER_ID)

    assert supervisor is not None and supervisor.roles == [Role.SUPERVISOR]
    assert doctor is not None and doctor.system_of_medicine == "allopathy"
    assert vaidya is not None and vaidya.system_of_medicine == "ayush"
    assert vaidya.roles == [Role.MEDICAL_OFFICER]
