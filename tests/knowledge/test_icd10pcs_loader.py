"""ICD-10-PCS loader: tables, definitions and the order file, on the fixture archives."""

import io
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import IcdTabularRow, KbSourceLoadRow, PcsRowRow, PcsTableRow
from app.knowledge.loaders.icd10pcs import (
    load_icd10pcs,
    parse_pcs_definitions,
    parse_pcs_tables,
)
from tests.knowledge import sources


def _bytes(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode())


def test_tables_give_the_first_three_axes_and_rows_of_valid_combinations() -> None:
    tables = {t.code: t for t in parse_pcs_tables(_bytes(sources.PCS_TABLES))}

    assert list(tables) == ["0DB", "0DT", "B20"]
    resection = tables["0DT"]
    assert resection.axes == [
        ("Section", "0", "Medical and Surgical"),
        ("Body System", "D", "Gastrointestinal System"),
        ("Operation", "T", "Resection"),
    ]
    assert resection.operation_definition == (
        "Cutting out or off, without replacement, all of a body part"
    )
    first, second = resection.rows
    assert first.axes == [
        ("Body Part", [("J", "Appendix"), ("K", "Ascending Colon")]),
        ("Approach", [("0", "Open"), ("4", "Percutaneous Endoscopic")]),
        ("Device", [("Z", "No Device")]),
        ("Qualifier", [("Z", "No Qualifier")]),
    ]
    assert (first.codes, second.codes) == (4, 1)
    assert tables["B20"].axes[2] == ("Type", "0", "Plain Radiography")
    assert tables["B20"].operation_definition is None


def test_definitions_are_keyed_by_section_axis_and_label() -> None:
    definitions = list(parse_pcs_definitions(_bytes(sources.PCS_DEFINITIONS)))
    by_key = {(d.section, d.pos, d.label): d for d in definitions}

    resection = by_key[("0", 3, "Resection")]
    assert resection.axis == "Operation"
    assert resection.definition == "Cutting out or off, without replacement, all of a body part"
    assert resection.includes == ["Total nephrectomy, total lobectomy of lung"]
    assert by_key[("0", 4, "Appendix")].includes == ["Vermiform appendix"]
    assert by_key[("0", 4, "Appendix")].definition is None
    assert by_key[("0", 5, "Percutaneous Endoscopic")].definition.startswith("Entry, by puncture")
    assert len(definitions) == 5  # the device aggregation table is not a definition


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    return sources.write_sources(tmp_path / "icd-2027")


async def _count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_load_writes_tables_rows_definitions_and_titles(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    report = await load_icd10pcs(sessions, source_dir)

    assert (report.system, report.version) == ("ICD-10-PCS", "FY2027")
    assert report.counts == {
        "tables": 3,
        "table rows": 4,
        "codes the tables make": 10,
        "definitions": 5,
        "order file codes": 13,
        "valid codes": 10,
    }
    # Every code the tables make is a valid code in the order file, and the reverse.
    assert report.checks == {
        "table codes missing from the order file": "0",
        "valid order codes no table makes": "0",
    }
    assert len(report.sample) == 10  # every valid code, as the fixture has fewer than 20
    async with sessions() as session:
        title = await session.scalar(
            select(IcdTabularRow).where(
                IcdTabularRow.system == "ICD-10-PCS", IcdTabularRow.code == "0DTJ4ZZ"
            )
        )
        loads = list(await session.scalars(select(KbSourceLoadRow)))
    assert title is not None and title.billable
    assert title.title == "Resection of Appendix, Percutaneous Endoscopic Approach"
    assert {row.member for row in loads} == {
        "icd10pcs_tables_2027.xml",
        "icd10pcs_definitions_2027.xml",
        "icd10pcs_order_2027.txt",
    }
    assert {row.system for row in loads} == {"ICD-10-PCS"}


async def test_loading_twice_keeps_one_copy(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    await load_icd10pcs(sessions, source_dir)
    await load_icd10pcs(sessions, source_dir)

    assert await _count(sessions, PcsTableRow) == 3
    assert await _count(sessions, PcsRowRow) == 4
    assert await _count(sessions, IcdTabularRow) == 13
    assert await _count(sessions, KbSourceLoadRow) == 3
