"""Shared fixtures. DB-backed tests run on a throwaway SQLite file per test. Tests marked pg
run on real Postgres (tests/pg/) and are skipped unless PG_TEST_DATABASE_URL is set."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("PG_TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="PG_TEST_DATABASE_URL is not set")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip)
