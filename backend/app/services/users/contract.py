"""Contract for looking up the Phase 1 seeded users. Real auth replaces this later."""

from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Role

STAFF_ROLES = frozenset(Role) - {Role.HOSPITAL_USER, Role.MEMBER}


class User(BaseModel):
    id: UUID
    name: str
    kind: Literal["staff", "member", "hospital"]
    roles: list[Role]
    # The approver limit. None when the user is not an approver or has no limit; check
    # roles to tell them apart.
    max_amount: Decimal | None = None
    hospital_id: str | None = None
    policy_number: str | None = None
    member_id: str | None = None


class UserService(Protocol):
    async def get(self, user_id: UUID) -> User | None: ...
