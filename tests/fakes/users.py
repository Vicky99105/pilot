from uuid import UUID

from app.services.users.contract import User


class FakeUserService:
    def __init__(self, users: list[User]) -> None:
        self.users = {u.id: u for u in users}

    async def get(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)
