"""Fixtures for tests that need real Postgres. Each test gets a new, empty database on the
server PG_TEST_DATABASE_URL names, dropped afterwards. Without that variable every test
marked pg is skipped (see tests/conftest.py)."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

PG_ENV = "PG_TEST_DATABASE_URL"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "backend" / "alembic.ini"


def _server() -> URL:
    return make_url(os.environ[PG_ENV]).set(drivername="postgresql+psycopg")


@pytest.fixture
def empty_database() -> Iterator[str]:
    """The URL of a new, empty database. Dropped when the test ends."""
    name = f"p21_{uuid4().hex[:12]}"
    admin = create_engine(_server(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _server().set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def alembic_config(url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Alembic reads the database URL from app settings, that is DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", url)
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def migrated(empty_database: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A database at alembic head."""
    command.upgrade(alembic_config(empty_database, monkeypatch), "head")
    return empty_database


@pytest.fixture
async def pg_sessions(migrated: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated, pool_size=20)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
