from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import UserRoleRow, UserRow
from app.services.users.contract import User
from app.shared.types import Role


class DefaultUserService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, user_id: UUID) -> User | None:
        async with self._sessions() as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                return None
            roles = list(
                await session.scalars(select(UserRoleRow).where(UserRoleRow.user_id == user_id))
            )
        approver = next((r for r in roles if r.role == Role.APPROVER.value), None)
        return User(
            id=row.id,
            name=row.name,
            kind=row.kind,
            roles=sorted(Role(r.role) for r in roles),
            max_amount=approver.max_amount if approver else None,
            hospital_id=row.hospital_id,
            policy_number=row.policy_number,
            member_id=row.member_id,
            system_of_medicine=row.system_of_medicine,
        )
