"""ICD-10-CM loader: parsing the tabular, order and index files, and loading them into the
knowledge tables. Runs on the small fixture archives in tests/knowledge/sources.py."""

import hashlib
import io
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import IcdIndexRow, IcdTabularRow, KbSourceLoadRow
from app.database.seed_icd_fixture import seed_icd_fixture
from app.knowledge.loaders.common import FY2027, SourceMissing
from app.knowledge.loaders.icd10cm import (
    load_icd10cm,
    parse_index,
    parse_order,
    parse_tabular,
)
from tests.knowledge import sources


def _bytes(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode())


def test_tabular_reads_every_code_with_its_notes_chapter_and_section() -> None:
    entries = {e.code: e for e in parse_tabular(_bytes(sources.CM_TABULAR))}

    assert set(entries) == {
        "E11", "E11.9", "K35", "K35.3", "K35.30", "K35.32", "K35.8", "K35.80", "K35.89",
        "K35.890", "K35.891", "K37", "S72", "S72.0", "S72.00", "S72.001",
    }  # fmt: skip
    k35_32 = entries["K35.32"]
    assert k35_32.notes == {
        "inclusion_terms": ["(Acute) appendicitis with perforation NOS", "Perforated appendix NOS"]
    }
    assert k35_32.chapter == "11. Diseases of the digestive system (K00-K95)"
    assert k35_32.section == "Diseases of appendix (K35-K38)"
    # Mixed content (the addenda's <new> tag) is read as plain text.
    assert entries["K35"].notes == {"code_also": ["if applicable, any associated sepsis (A41.-)"]}
    assert entries["E11"].notes == {
        "use_additional": ["code to identify control using insulin (Z79.4)"],
        "excludes1": ["diabetes mellitus due to underlying condition (E08.-)"],
    }
    s72 = entries["S72"]
    assert s72.notes["code_first"] == ["any associated open wound"]
    assert s72.notes["notes"] == [
        "A fracture not indicated as displaced or nondisplaced should be coded to displaced"
    ]
    assert s72.notes["seven_chr_note"] == [
        "The appropriate 7th character is to be added to all codes from category S72"
    ]
    # The 7th character definitions apply to every code below the category.
    assert entries["S72.001"].seven_chr_def == {
        "A": "initial encounter for closed fracture",
        "D": "subsequent encounter for closed fracture with routine healing",
    }
    assert entries["K35.32"].seven_chr_def == {}


def test_order_file_gives_every_code_its_flag_and_long_title() -> None:
    lines = list(parse_order(_bytes(sources.order_file(sources.CM_ORDER))))

    assert len(lines) == len(sources.CM_ORDER)
    s72_001a = next(line for line in lines if line.code == "S72001A")
    assert s72_001a.valid is True
    assert s72_001a.title == (
        "Fracture of unspecified part of neck of right femur, initial encounter for closed fracture"
    )
    assert next(line for line in lines if line.code == "K35").valid is False


def test_index_gives_full_paths_codes_and_references() -> None:
    entries = list(parse_index(_bytes(sources.CM_INDEX), "diseases"))
    by_path = {e.path: e for e in entries}

    acute = by_path["Appendicitis > acute"]
    assert (acute.term, acute.code, acute.index_name) == ("Appendicitis", "K35.80", "diseases")
    # Nonessential modifiers are searchable but stay out of the path.
    assert "catarrhal" in acute.search_text and "pneumococcal" in acute.search_text
    assert by_path["Appendicitis > with > perforation NOS"].code == "K35.32"
    assert by_path["Appendicitis > acute > specified NEC"].code == "K35.89-"
    assert by_path["Appendicitis"].code == "K37"
    assert by_path["Abandonment"].see == "Maltreatment"
    assert by_path["Abandonment"].code is None
    assert by_path["Appendicitis > chronic"].see_also == "Appendicitis, recurrent"
    amyotrophy = by_path["Diabetes, diabetic > with > amyotrophy"]
    assert (amyotrophy.code, amyotrophy.manifestation) == ("E11.44", "G73.3")
    assert by_path["Fracture, traumatic > femur, femoral > neck"].code == "S72.00-"
    # A sub-term with neither a code nor a reference ("with") is not an entry of its own.
    assert "Appendicitis > with" not in by_path
    assert "Fracture, traumatic" not in by_path


def test_title_text_after_a_modifier_stays_in_the_path() -> None:
    entries = list(parse_index(_bytes(sources.CM_EINDEX), "external_causes"))
    [accident] = [e for e in entries if e.code]
    [automobile] = [e for e in entries if e.see]

    assert (accident.path, accident.code) == ("Accident NEC", "X58")
    assert automobile.path == "Accident NEC > automobile"
    assert automobile.see == "Accident, transport, car occupant"
    assert automobile.index_name == "external_causes"


def test_drug_and_neoplasm_tables_give_one_entry_per_cell_with_its_column() -> None:
    drugs = list(parse_index(_bytes(sources.CM_DRUG), "drugs"))
    paracetamol = [d for d in drugs if d.path == "Paracetamol"]
    assert [(d.table_column, d.code) for d in paracetamol] == [
        ("Poisoning Accidental (unintentional)", "T39.1X1"),
        ("Poisoning Intentional self-harm", "T39.1X2"),
        ("Poisoning Assault", "T39.1X3"),
        ("Poisoning Undetermined", "T39.1X4"),
        ("Adverse effect", "T39.1X5"),
        ("Underdosing", "T39.1X6"),
    ]
    # "--" marks a cell with no code.
    assert [d.code for d in drugs if d.path == "Petrol"] == [
        "T52.0X1", "T52.0X2", "T52.0X3", "T52.0X4",
    ]  # fmt: skip

    neoplasms = list(parse_index(_bytes(sources.CM_NEOPLASM), "neoplasms"))
    appendix = [n for n in neoplasms if n.path == "Neoplasm, neoplastic > appendix"]
    assert [(n.table_column, n.code) for n in appendix if n.code] == [
        ("Malignant Primary", "C18.1"),
        ("Malignant Secondary", "C78.5"),
        ("Ca in situ", "D01.0"),
        ("Benign", "D12.1"),
        ("Uncertain Behavior", "D37.3"),
    ]
    [reference] = [n for n in appendix if n.code is None]
    assert reference.see_also == "Neoplasm, intestine"


def test_external_entities_are_refused() -> None:
    hostile = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        "<ICD10CM.index><letter><title>A</title><mainTerm><title>&e;</title>"
        "<code>A00</code></mainTerm></letter></ICD10CM.index>"
    )
    with pytest.raises(Exception, match=r"(?i)dtd|entit"):
        list(parse_index(_bytes(hostile), "diseases"))


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    return sources.write_sources(tmp_path / "icd-2027")


async def _count(sessions: async_sessionmaker[AsyncSession], model: type, **where: str) -> int:
    async with sessions() as session:
        query = select(func.count()).select_from(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return int(await session.scalar(query) or 0)


async def test_load_writes_tabular_and_index_rows_with_source_and_version(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    report = await load_icd10cm(sessions, source_dir)

    assert (report.system, report.version) == ("ICD-10-CM", "FY2027")
    assert report.counts == {
        "tabular codes": 18,
        "billable codes": 9,
        "index: diseases": 9,
        "index: external_causes": 2,
        "index: drugs": 10,
        "index: neoplasms": 12,
    }
    async with sessions() as session:
        s72 = await session.scalar(select(IcdTabularRow).where(IcdTabularRow.code == "S72.001A"))
        k35_32 = await session.scalar(select(IcdTabularRow).where(IcdTabularRow.code == "K35.32"))
        k35 = await session.scalar(select(IcdTabularRow).where(IcdTabularRow.code == "K35"))
    # A 7th character code is in the order file only: it takes the category's chapter,
    # section and 7th character definitions, and its parent is the six character code.
    assert s72 is not None and s72.billable and s72.parent == "S72.001"
    assert s72.version == FY2027.version and s72.source_version == FY2027.version
    assert s72.section == "Injuries to the hip and thigh (S70-S79)"
    assert s72.notes["seven_chr_def"]["A"] == "initial encounter for closed fracture"
    assert k35_32 is not None and k35_32.parent == "K35.3" and k35_32.billable
    assert k35_32.notes["inclusion_terms"][1] == "Perforated appendix NOS"
    assert k35 is not None and k35.billable is False and k35.parent is None


async def test_load_records_each_source_file_with_its_hash(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    report = await load_icd10cm(sessions, source_dir)

    tables = hashlib.sha256((source_dir / sources.CM_TABLES_ZIP).read_bytes()).hexdigest()
    async with sessions() as session:
        loads = list(await session.scalars(select(KbSourceLoadRow)))
    by_member = {row.member: row for row in loads}
    assert set(by_member) == {
        "Table and Index/icd10cm_tabular_2027.xml",
        "Code Descriptions/icd10cm_order_2027.txt",
        "Table and Index/icd10cm_index_2027.xml",
        "Table and Index/icd10cm_eindex_2027.xml",
        "Table and Index/icd10cm_drug_2027.xml",
        "Table and Index/icd10cm_neoplasm_2027.xml",
    }
    index = by_member["Table and Index/icd10cm_index_2027.xml"]
    assert (index.file_name, index.sha256, index.rows) == (sources.CM_TABLES_ZIP, tables, 9)
    assert (index.effective_from.isoformat(), index.effective_to.isoformat()) == (
        "2026-10-01",
        "2027-09-30",
    )
    assert {f.sha256 for f in report.files if f.file_name == sources.CM_TABLES_ZIP} == {tables}
    assert len(report.sample) == 20
    assert report.checks == {
        "tabular codes missing from the order file": "0",
        "order codes with no tabular entry or ancestor": "0",
    }


async def test_loading_twice_replaces_the_rows_and_removes_the_fixture(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    await seed_icd_fixture(sessions)
    assert await _count(sessions, IcdTabularRow, source="fixture") > 0

    await load_icd10cm(sessions, source_dir)
    await load_icd10cm(sessions, source_dir)

    assert await _count(sessions, IcdTabularRow) == 18
    assert await _count(sessions, IcdIndexRow) == 33
    assert await _count(sessions, IcdTabularRow, source="fixture") == 0
    assert await _count(sessions, IcdIndexRow, source="fixture") == 0
    assert await _count(sessions, KbSourceLoadRow) == 6


async def test_a_missing_source_file_stops_the_load_and_changes_nothing(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> None:
    await load_icd10cm(sessions, source_dir)
    (source_dir / sources.CM_ORDER_ZIP).unlink()

    with pytest.raises(SourceMissing, match=re.escape(sources.CM_ORDER_ZIP)):
        await load_icd10cm(sessions, source_dir)

    assert await _count(sessions, IcdTabularRow) == 18
