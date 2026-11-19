"""Knowledge base lookups through the contract, on the seeded ICD-10-CM fixture slice."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.services.knowledge_base.contract import (
    ICD10CM,
    ICD10CM_FIXTURE_VERSION,
    MAX_INDEX_HITS,
    KnowledgeBase,
)
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.shared.seed_ids import PRODUCT_UIN


@pytest.fixture
async def kb(sessions: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    await seed_claims_db(sessions)
    return DefaultKnowledgeBase(sessions)


async def test_search_finds_appendicitis_entries_with_their_sub_term_paths(
    kb: KnowledgeBase,
) -> None:
    hits = await kb.search_icd_index("appendicitis")

    paths = {h.path: h.code for h in hits}
    assert paths["Appendicitis > acute"] == "K35.80"
    assert paths["Appendicitis > acute > with perforation > with localized peritonitis"] == (
        "K35.32"
    )
    assert all("appendicitis" in h.path.lower() for h in hits)
    assert 1 <= len(hits) <= MAX_INDEX_HITS


async def test_search_needs_every_word_and_ignores_case_and_order(kb: KnowledgeBase) -> None:
    hits = await kb.search_icd_index("PERFORATION acute Appendicitis")

    assert {h.code for h in hits} == {"K35.32", "K35.33"}


async def test_search_returns_nothing_for_an_unknown_term(kb: KnowledgeBase) -> None:
    assert await kb.search_icd_index("dragon pox") == []
    assert await kb.search_icd_index("  ") == []


async def test_tabular_gives_title_billable_flag_and_notes(kb: KnowledgeBase) -> None:
    entry = await kb.get_icd_tabular("k35.32")

    assert entry is not None
    assert entry.code.system == ICD10CM
    assert entry.code.version == ICD10CM_FIXTURE_VERSION
    assert entry.code.code == "K35.32"
    assert entry.code.display == (
        "Acute appendicitis with perforation and localized peritonitis, without abscess"
    )
    assert entry.billable is True
    assert entry.source == "fixture"

    category = await kb.get_icd_tabular("E11")
    assert category is not None and category.billable is False
    assert any("Z79.4" in note for note in category.use_additional)


async def test_tabular_returns_none_for_a_code_not_loaded(kb: KnowledgeBase) -> None:
    assert await kb.get_icd_tabular("Z99.999") is None


async def test_get_clause_reads_the_policy_clause(kb: KnowledgeBase) -> None:
    clause = await kb.get_clause(PRODUCT_UIN, "Excl03")

    assert clause is not None and clause.title == "First 30 days"
    assert await kb.get_clause(PRODUCT_UIN, "NOPE") is None


async def test_seeding_twice_keeps_one_copy_of_the_fixture(
    sessions: async_sessionmaker[AsyncSession], kb: KnowledgeBase
) -> None:
    before = await kb.search_icd_index("appendicitis")
    await seed_claims_db(sessions)

    assert len(await kb.search_icd_index("appendicitis")) == len(before)
