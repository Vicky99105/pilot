"""Knowledge base lookups through the contract: first on the hand-made ICD-10-CM fixture
slice, then on the fixture CMS archives loaded through the real loaders."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.database.seed_icd_fixture import seed_icd_fixture
from app.knowledge.loaders.common import Release
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.services.knowledge_base.contract import (
    ICD10CM,
    ICD10CM_FIXTURE_VERSION,
    ICD10PCS,
    MAX_INDEX_HITS,
    KnowledgeBase,
)
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.shared.seed_ids import PRODUCT_UIN
from tests.knowledge import sources


@pytest.fixture
async def kb(sessions: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    await seed_claims_db(sessions)
    await seed_icd_fixture(sessions)
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
    await seed_icd_fixture(sessions)

    assert len(await kb.search_icd_index("appendicitis")) == len(before)


# --- On the CMS archives loaded through the loaders ---------------------------------------


@pytest.fixture
async def loaded(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> KnowledgeBase:
    source_dir = sources.write_sources(tmp_path / "icd-2027")
    await load_icd10cm(sessions, source_dir)
    await load_icd10pcs(sessions, source_dir)
    return DefaultKnowledgeBase(sessions)


async def test_search_reads_sub_terms_and_nonessential_modifiers(loaded: KnowledgeBase) -> None:
    hits = await loaded.search_icd_index("appendicitis catarrhal")

    assert [(h.path, h.code) for h in hits] == [
        ("Appendicitis > acute", "K35.80"),
        ("Appendicitis > acute > specified NEC", "K35.89-"),
    ]
    [chronic] = await loaded.search_icd_index("appendicitis chronic")
    assert (chronic.code, chronic.see_also) == (None, "Appendicitis, recurrent")
    [abandonment] = await loaded.search_icd_index("abandonment")
    assert abandonment.see == "Maltreatment"


async def test_search_reads_one_index_at_a_time(loaded: KnowledgeBase) -> None:
    assert await loaded.search_icd_index("paracetamol") == []

    drugs = await loaded.search_icd_index("paracetamol", "drugs")
    assert {(h.column, h.code) for h in drugs} >= {("Adverse effect", "T39.1X5")}
    assert all(h.index == "drugs" for h in drugs)
    neoplasms = await loaded.search_icd_index("neoplasm appendix", "neoplasms")
    assert ("Malignant Primary", "C18.1") in {(h.column, h.code) for h in neoplasms}
    [automobile] = await loaded.search_icd_index("automobile", "external_causes")
    assert automobile.see == "Accident, transport, car occupant"


async def test_tabular_gives_every_note_the_hierarchy_and_the_version(
    loaded: KnowledgeBase,
) -> None:
    k35_32 = await loaded.get_icd_tabular("K35.32")
    assert k35_32 is not None
    assert (k35_32.code.system, k35_32.code.version) == (ICD10CM, "FY2027")
    assert k35_32.inclusion_terms == [
        "(Acute) appendicitis with perforation NOS",
        "Perforated appendix NOS",
    ]
    assert (k35_32.parent, k35_32.section) == ("K35.3", "Diseases of appendix (K35-K38)")
    assert k35_32.source == "cms"

    k35 = await loaded.get_icd_tabular("K35")
    assert k35 is not None and not k35.billable
    assert k35.children == ["K35.3", "K35.8"]
    assert k35.code_also == ["if applicable, any associated sepsis (A41.-)"]
    s72 = await loaded.get_icd_tabular("S72")
    assert s72 is not None
    assert s72.code_first == ["any associated open wound"]
    assert s72.notes and s72.seven_chr_note
    s72_001a = await loaded.get_icd_tabular("s72.001a")
    assert s72_001a is not None and s72_001a.billable
    assert s72_001a.seven_chr_def["A"] == "initial encounter for closed fracture"


async def test_tabular_gives_pcs_titles(loaded: KnowledgeBase) -> None:
    entry = await loaded.get_icd_tabular("0DTJ4ZZ", system=ICD10PCS)

    assert entry is not None and entry.billable
    assert entry.code.display == "Resection of Appendix, Percutaneous Endoscopic Approach"
    assert entry.code.system == ICD10PCS
    assert await loaded.get_icd_tabular("0DTJ4ZZ") is None  # not an ICD-10-CM code


async def test_pcs_options_give_each_next_character_with_label_and_definition(
    loaded: KnowledgeBase,
) -> None:
    sections = await loaded.pcs_options("")
    assert [(o.position, o.axis, o.value, o.label) for o in sections] == [
        (1, "Section", "0", "Medical and Surgical"),
        (1, "Section", "B", "Imaging"),
    ]
    operations = await loaded.pcs_options("0D")
    assert [(o.value, o.label) for o in operations] == [("B", "Excision"), ("T", "Resection")]
    resection = operations[1]
    assert resection.definition == "Cutting out or off, without replacement, all of a body part"
    assert resection.includes == ["Total nephrectomy, total lobectomy of lung"]

    body_parts = await loaded.pcs_options("0dt")
    assert [(o.position, o.axis, o.value, o.label) for o in body_parts] == [
        (4, "Body Part", "J", "Appendix"),
        (4, "Body Part", "K", "Ascending Colon"),
    ]
    assert body_parts[0].includes == ["Vermiform appendix"]
    approaches = await loaded.pcs_options("0DTJ")
    assert [o.value for o in approaches] == ["0", "4"]
    assert approaches[1].definition is not None
    # Rows combine: only the second row of 0DT allows the hand-assisted qualifier.
    assert [o.value for o in await loaded.pcs_options("0DTJ4Z")] == ["G", "Z"]
    assert [o.value for o in await loaded.pcs_options("0DTJ0Z")] == ["Z"]
    assert [o.value for o in await loaded.pcs_options("0DTK4Z")] == ["Z"]
    assert await loaded.pcs_options("0DTJ4ZZ") == []
    assert await loaded.pcs_options("0DX") == []


async def test_pcs_validate_accepts_only_codes_a_table_row_makes(loaded: KnowledgeBase) -> None:
    assert await loaded.pcs_validate("0DTJ4ZZ") is True
    assert await loaded.pcs_validate("0dtj4zg") is True
    assert await loaded.pcs_validate("0DTK4ZG") is False  # G is valid with J only
    assert await loaded.pcs_validate("0DTJ4Z") is False
    assert await loaded.pcs_validate("0DTJ4ZZZ") is False
    assert await loaded.pcs_validate("") is False


def test_code_in_ranges_covers_codes_below_each_end(kb: KnowledgeBase) -> None:
    assert kb.code_in_ranges("K35.80", ["K35-K38"])
    assert kb.code_in_ranges("K38.9", ["K35-K38"])
    assert kb.code_in_ranges("k36", ["K35-K38"])
    assert not kb.code_in_ranges("K39", ["K35-K38"])
    assert not kb.code_in_ranges("K34.1", ["K35-K38"])
    assert kb.code_in_ranges("E11.65", ["I10", "E11.-"])
    assert kb.code_in_ranges("E11", ["E11"])
    assert not kb.code_in_ranges("E110", ["E11.1"])
    assert kb.code_in_ranges("O10.02", ["O10-O16"])
    assert kb.code_in_ranges("0DTJ4ZZ", ["0DT"])
    assert not kb.code_in_ranges("K35.80", [])


async def test_lookups_read_the_version_in_force_on_the_date(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    source_dir = sources.write_sources(tmp_path / "icd")
    fy2026 = Release("FY2026", date(2025, 10, 1), date(2026, 9, 30))
    await load_icd10cm(sessions, source_dir, fy2026)
    await load_icd10cm(sessions, source_dir)
    kb = DefaultKnowledgeBase(sessions)

    async def version(as_of: date | None) -> str:
        entry = await kb.get_icd_tabular("K35.80", as_of=as_of)
        assert entry is not None
        return entry.code.version

    assert await version(date(2026, 3, 1)) == "FY2026"
    assert await version(date(2026, 10, 1)) == "FY2027"
    assert await version(None) == "FY2027"
    assert await version(date(2024, 1, 1)) == "FY2027"  # nothing covers it: the latest
    hits = await kb.search_icd_index("appendicitis acute", as_of=date(2026, 3, 1))
    assert hits and len(hits) == len(await kb.search_icd_index("appendicitis acute"))
