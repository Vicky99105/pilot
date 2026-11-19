"""ICD index search on real Postgres: the trigram index serves the ILIKE filter, and hits
are ranked by trigram similarity."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed_icd_fixture import seed_icd_fixture
from app.services.knowledge_base.service import DefaultKnowledgeBase

pytestmark = pytest.mark.pg


async def test_search_ranks_the_closest_index_path_first(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_icd_fixture(pg_sessions)
    kb = DefaultKnowledgeBase(pg_sessions)

    hits = await kb.search_icd_index("appendicitis acute perforation localized peritonitis")

    assert hits[0].code == "K35.32"
    assert hits[0].path == ("Appendicitis > acute > with perforation > with localized peritonitis")
    assert await kb.search_icd_index("dragon pox") == []


async def test_the_trigram_index_serves_the_ilike_filter(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_icd_fixture(pg_sessions)
    async with pg_sessions() as session:
        await session.execute(text("SET enable_seqscan = off"))
        plan = await session.scalars(
            text("EXPLAIN SELECT * FROM icd_index WHERE search_text ILIKE '%appendic%'")
        )
        lines = "\n".join(plan)

    assert "ix_icd_index_search_trgm" in lines, lines
