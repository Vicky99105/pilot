"""Fake implementations of service contracts, for tests only."""

from app.services.health.contract import ComponentStatus


class FakeHealthService:
    """Implements the HealthService contract with fixed results."""

    def __init__(
        self,
        database: ComponentStatus | None = None,
        temporal: ComponentStatus | None = None,
    ) -> None:
        self._database = database or ComponentStatus(status="ok")
        self._temporal = temporal or ComponentStatus(status="ok")

    async def check_database(self) -> ComponentStatus:
        return self._database

    async def check_temporal(self) -> ComponentStatus:
        return self._temporal
