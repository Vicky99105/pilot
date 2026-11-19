from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import DocumentChecklistRow, HospitalRow, UserRoleRow, UserRow
from app.database.seed import seed_claims_db
from app.shared.seed_ids import DOCTOR_USER_ID, OPS_USER_ID


async def _count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_seed_is_idempotent_and_creates_five_users_and_hospitals(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    await seed_claims_db(sessions)

    assert await _count(sessions, UserRow) == 5
    assert await _count(sessions, HospitalRow) == 5
    assert await _count(sessions, DocumentChecklistRow) == 10


async def test_seed_gives_ops_and_doctor_their_roles(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)

    async with sessions() as session:
        rows = (await session.scalars(select(UserRoleRow))).all()
    roles = {(r.user_id, r.role): r.approval_limit for r in rows}

    assert {role for uid, role in roles if uid == OPS_USER_ID} == {
        "claims_operator",
        "bill_processor",
        "investigator",
    }
    assert {role for uid, role in roles if uid == DOCTOR_USER_ID} == {
        "medical_officer",
        "approver",
        "crc_reviewer",
    }
    assert roles[(DOCTOR_USER_ID, "approver")] == 100000


async def test_seeded_hospitals_have_13_digit_rohini_ids(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)

    async with sessions() as session:
        ids = (await session.scalars(select(HospitalRow.rohini_id))).all()

    assert all(len(i) == 13 and i.isdigit() for i in ids)
