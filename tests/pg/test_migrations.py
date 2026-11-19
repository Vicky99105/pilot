"""Migrations on real Postgres: from an empty database to head, down to base and up again."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.database.models import Base
from tests.pg.conftest import alembic_config

pytestmark = pytest.mark.pg


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _indexes(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT indexname FROM pg_indexes"))
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def test_upgrade_from_empty_then_downgrade_to_base_then_upgrade_again(
    empty_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = alembic_config(empty_database, monkeypatch)
    model_tables = set(Base.metadata.tables)

    command.upgrade(config, "head")
    assert model_tables <= _tables(empty_database)
    assert "ix_icd_index_search_trgm" in _indexes(empty_database)

    command.downgrade(config, "base")
    assert _tables(empty_database) <= {"alembic_version"}

    command.upgrade(config, "head")
    assert model_tables <= _tables(empty_database)
