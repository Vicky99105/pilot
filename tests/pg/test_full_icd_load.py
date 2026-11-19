"""The real loaders on the full CMS FY2027 release, on real Postgres: row counts in plausible
ranges, known entries, and every lookup under the 50 ms budget.

Needs PG_TEST_DATABASE_URL and the downloaded release in KB_SOURCES_DIR (default
.local/kb_sources/icd-2027). Skipped when either is missing. The release is loaded once for
the module; each test reads it.
"""

import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.seed_icd_fixture import seed_icd_fixture
from app.knowledge.loaders.common import LoadReport
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.services.knowledge_base.contract import ICD10PCS
from app.services.knowledge_base.service import DefaultKnowledgeBase
from tests.pg.conftest import ALEMBIC_INI, PG_ENV, _server

pytestmark = pytest.mark.pg

SOURCE_DIR = Path(
    os.environ.get(
        "KB_SOURCES_DIR",
        str(Path(__file__).resolve().parents[2] / ".local" / "kb_sources" / "icd-2027"),
    )
)
BUDGET_MS = 50.0
RUNS = 7

if not SOURCE_DIR.is_dir():
    pytestmark = [pytest.mark.pg, pytest.mark.skip(reason=f"no ICD release at {SOURCE_DIR}")]


class Loaded:
    def __init__(self, url: str, cm: LoadReport, pcs: LoadReport) -> None:
        self.url, self.cm, self.pcs = url, cm, pcs


@pytest.fixture(scope="module")
def loaded() -> Iterator[Loaded]:
    """A new database at head, with the fixture seeded and then the full release loaded."""
    if not os.environ.get(PG_ENV):
        pytest.skip(f"{PG_ENV} is not set")
    name = f"p22_{uuid4().hex[:12]}"
    admin = create_engine(_server(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _server().set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        from alembic.config import Config

        command.upgrade(Config(str(ALEMBIC_INI)), "head")

        async def load() -> tuple[LoadReport, LoadReport]:
            engine = create_async_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                await seed_icd_fixture(sessions)
                cm = await load_icd10cm(sessions, SOURCE_DIR)
                pcs = await load_icd10pcs(sessions, SOURCE_DIR)
                return cm, pcs
            finally:
                await engine.dispose()

        cm, pcs = asyncio.run(load())  # the loaders refresh the planner statistics
        yield Loaded(url, cm, pcs)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
async def kb(loaded: Loaded) -> Any:
    engine = create_async_engine(loaded.url)
    yield DefaultKnowledgeBase(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


def test_row_counts_are_in_plausible_ranges(loaded: Loaded) -> None:
    cm, pcs = loaded.cm.counts, loaded.pcs.counts
    print("\n" + "\n".join(loaded.cm.lines() + loaded.pcs.lines()))

    assert 95_000 <= cm["tabular codes"] <= 102_000
    assert 72_000 <= cm["billable codes"] <= 78_000
    assert 70_000 <= cm["index: diseases"] <= 100_000
    assert 5_000 <= cm["index: external_causes"] <= 12_000
    assert 30_000 <= cm["index: drugs"] <= 50_000
    assert 8_000 <= cm["index: neoplasms"] <= 15_000
    assert loaded.cm.checks == {
        "tabular codes missing from the order file": "0",
        "order codes with no tabular entry or ancestor": "0",
    }
    assert 850 <= pcs["tables"] <= 1_000
    assert 75_000 <= pcs["valid codes"] <= 85_000
    assert pcs["codes the tables make"] == pcs["valid codes"]
    assert loaded.pcs.checks == {
        "table codes missing from the order file": "0",
        "valid order codes no table makes": "0",
    }
    assert len(loaded.cm.sample) == 20 and len(loaded.pcs.sample) == 20


async def test_known_icd10cm_entries(kb: DefaultKnowledgeBase, loaded: Loaded) -> None:
    k35_890 = await kb.get_icd_tabular("K35.890")
    assert k35_890 is not None and k35_890.billable
    assert k35_890.code.display == "Other acute appendicitis without perforation or gangrene"
    assert k35_890.code.version == "FY2027" and k35_890.source == "cms"
    assert k35_890.parent == "K35.89"
    k35 = await kb.get_icd_tabular("K35")
    assert k35 is not None and not k35.billable and "K35.8" in k35.children

    hits = await kb.search_icd_index("appendicitis acute")
    assert ("Appendicitis > acute", "K35.80") in [(h.path, h.code) for h in hits]
    assert all(h.code is None or h.code.startswith("K3") for h in hits)
    # Ranking reads the path, not the nonessential modifiers: the main term and its
    # direct sub-terms come first.
    [first, *_] = await kb.search_icd_index("appendicitis")
    assert (first.path, first.code) == ("Appendicitis", "K37")
    assert ("Appendicitis > acute", "K35.80") in [
        (h.path, h.code) for h in await kb.search_icd_index("appendicitis")
    ]
    # A word found only among the modifiers still matches the entry it qualifies.
    [best, *_] = await kb.search_icd_index("appendicitis acute suppurative")
    assert (best.path, best.code) == ("Appendicitis > acute", "K35.80")
    # The fixture is gone once the release is loaded.
    engine = create_engine(loaded.url)
    with engine.connect() as conn:
        fixture = conn.execute(
            text("SELECT count(*) FROM icd_tabular WHERE source = 'fixture'")
        ).scalar_one()
    engine.dispose()
    assert fixture == 0


async def test_known_icd10pcs_entries(kb: DefaultKnowledgeBase) -> None:
    assert await kb.pcs_validate("0DTJ4ZZ") is True
    assert await kb.pcs_validate("0DTJ4ZX") is False
    resections = await kb.pcs_options("0DT")
    appendix = next(o for o in resections if o.value == "J")
    assert (appendix.position, appendix.axis, appendix.label) == (4, "Body Part", "Appendix")
    operations = await kb.pcs_options("0D")
    resection = next(o for o in operations if o.value == "T")
    assert resection.label == "Resection" and resection.definition
    approaches = await kb.pcs_options("0DTJ")
    assert ("4", "Percutaneous Endoscopic") in [(o.value, o.label) for o in approaches]
    title = await kb.get_icd_tabular("0DTJ4ZZ", system=ICD10PCS)
    assert title is not None
    assert title.code.display == "Resection of Appendix, Percutaneous Endoscopic Approach"


async def _median_ms(call: Callable[[], Awaitable[Any]]) -> float:
    await call()  # warm the connection and the cache
    times = []
    for _ in range(RUNS):
        started = time.perf_counter()
        await call()
        times.append((time.perf_counter() - started) * 1000)
    return statistics.median(times)


async def test_every_lookup_runs_under_the_budget(kb: DefaultKnowledgeBase) -> None:
    lookups: dict[str, Callable[[], Awaitable[Any]]] = {
        "search appendicitis acute": lambda: kb.search_icd_index("appendicitis acute"),
        "search acute appendicitis perforation peritonitis": lambda: kb.search_icd_index(
            "appendicitis acute perforation localized peritonitis"
        ),
        "search pain abdominal": lambda: kb.search_icd_index("pain abdominal"),
        "search diabetes type 2 hyperglycemia": lambda: kb.search_icd_index(
            "diabetes type 2 hyperglycemia"
        ),
        "search drugs paracetamol": lambda: kb.search_icd_index("paracetamol", "drugs"),
        "search neoplasms colon": lambda: kb.search_icd_index("colon", "neoplasms"),
        "get_icd_tabular K35.890": lambda: kb.get_icd_tabular("K35.890"),
        "get_icd_tabular S72 (many children)": lambda: kb.get_icd_tabular("S72.0"),
        "pcs_options ''": lambda: kb.pcs_options(""),
        "pcs_options 0": lambda: kb.pcs_options("0"),
        "pcs_options 0D": lambda: kb.pcs_options("0D"),
        "pcs_options 0DT": lambda: kb.pcs_options("0DT"),
        "pcs_options 0DTJ4Z": lambda: kb.pcs_options("0DTJ4Z"),
        "pcs_validate 0DTJ4ZZ": lambda: kb.pcs_validate("0DTJ4ZZ"),
    }
    timings = {name: await _median_ms(call) for name, call in lookups.items()}
    print("\nLookup timings, median of", RUNS, "runs (ms):")
    for name, ms in timings.items():
        print(f"  {name}: {ms:.2f}")

    slow = {name: round(ms, 1) for name, ms in timings.items() if ms >= BUDGET_MS}
    assert not slow, slow


async def test_index_search_uses_the_trigram_index_on_the_full_data(loaded: Loaded) -> None:
    engine = create_async_engine(loaded.url)
    try:
        async with async_sessionmaker(engine)() as session:
            plan = await _explain(session)
    finally:
        await engine.dispose()
    print("\n" + plan)
    assert "ix_icd_index_search_trgm" in plan, plan


async def _explain(session: AsyncSession) -> str:
    rows = await session.scalars(
        text(
            "EXPLAIN ANALYZE SELECT * FROM icd_index WHERE system = 'ICD-10-CM' "
            "AND index_name = 'diseases' AND version = 'FY2027' "
            "AND search_text ILIKE '%appendicitis%' AND search_text ILIKE '%acute%' "
            "ORDER BY similarity(lower(path), 'appendicitis acute') DESC, "
            "length(path) LIMIT 15"
        )
    )
    return "\n".join(rows)
