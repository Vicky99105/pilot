"""Writes the override log. Services that store a person's correction call append."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import OverrideRow, utcnow
from app.services.overrides.contract import OverrideEntry


def user_of(actor: str) -> UUID | None:
    """The user an actor string names ("user:<id>"), if any."""
    if not actor.startswith("user:"):
        return None
    try:
        return UUID(actor.removeprefix("user:"))
    except ValueError:
        return None


class OverrideRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, entries: list[OverrideEntry]) -> None:
        if not entries:
            return
        now = utcnow()
        async with self._sessions.begin() as session:
            for entry in entries:
                dumped = entry.model_dump(mode="json")
                session.add(
                    OverrideRow(
                        id=uuid4(),
                        claim_id=entry.claim_id,
                        stage=entry.stage.value,
                        kind=entry.kind,
                        field=entry.field,
                        before=dumped["before"],
                        after=dumped["after"],
                        reason=entry.reason,
                        actor=entry.actor,
                        user_id=user_of(entry.actor),
                        created_at=now,
                    )
                )
