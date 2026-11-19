"""Contract for the health service. Callers import this module only."""

from typing import Literal, Protocol

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    status: Literal["ok", "down"]
    detail: str | None = None


class HealthService(Protocol):
    async def check_database(self) -> ComponentStatus: ...

    async def check_temporal(self) -> ComponentStatus: ...
