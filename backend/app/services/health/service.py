"""Default implementation of the HealthService contract."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

from app.config import Settings
from app.services.health.contract import ComponentStatus


class DefaultHealthService:
    def __init__(
        self, settings: Settings, engine: AsyncEngine, timeout_seconds: float = 3.0
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._timeout = timeout_seconds
        self._client: Client | None = None

    async def check_database(self) -> ComponentStatus:
        try:
            async with asyncio.timeout(self._timeout), self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return _down(exc)
        return ComponentStatus(status="ok")

    async def check_temporal(self) -> ComponentStatus:
        try:
            async with asyncio.timeout(self._timeout):
                client = await self._get_client()
                await client.workflow_service.describe_namespace(
                    DescribeNamespaceRequest(namespace=self._settings.temporal_namespace)
                )
        except Exception as exc:
            return _down(exc)
        return ComponentStatus(status="ok")

    async def _get_client(self) -> Client:
        # The client reconnects on its own after the first connect succeeds.
        if self._client is None:
            self._client = await Client.connect(
                self._settings.temporal_address, namespace=self._settings.temporal_namespace
            )
        return self._client


def _down(exc: Exception) -> ComponentStatus:
    return ComponentStatus(status="down", detail=f"{type(exc).__name__}: {exc}"[:300])
