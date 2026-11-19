"""Drug ceiling prices and the essential medicines list.

- NPPA S.O. 1575(E) of 25.03.2026: the annual WPI revision of the ceiling prices of 767
  NLEM 2022 formulations, in force from 01.04.2026. The English table (pp.31-51) is read with
  pdfplumber; each row is a formulation, its unit, the revised ceiling price per unit (before
  GST) and the S.O. that first fixed it. Serial numbers must run 1 to 767.
- NLEM 2022 (the NHSRC report, pp.4-84): each medicine with its number, section, levels of
  care (P, S, T) and formulations. The Gazette copy is the legal reference; the report is the
  cleaner text to parse.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import DrugPriceRow, NlemMedicineRow
from app.knowledge.loaders.common import LoadReport, Sampler, analyze
from app.knowledge.loaders.hbp import join_cell
from app.knowledge.loaders.pdf import (
    NOT_STATED,
    ReferenceRelease,
    Table,
    amount,
    clean,
    clear_release,
    lines_of,
    page_tables,
    page_texts,
    percent,
    record_release,
    source_file,
    source_path,
)

NPPA = "s7_nppa_wpi2026_nlem2022/nppa_wpi_2026-03-27_767_nlem2022.pdf"
NPPA_PAGES = range(31, 52)
NPPA_EXPECTED = 767
NOTIFICATION = "S.O. 1575(E) dated 25.03.2026 (WPI revision, in force 01.04.2026)"
NLEM = "s7_nlem/nlem2022.pdf"
NLEM_PAGES = range(4, 85)
PRICES = ReferenceRelease("drugs", "NPPA-2026-04-01", date(2026, 4, 1))
NLEM_RELEASE = ReferenceRelease("nlem", "NLEM2022", NOT_STATED)

# A medicine's number: section 1 to 27, two to four levels, then the name (capitalised) or
# nothing (the name wraps to the next line). "0.5 mg" is a strength, not a number.
_NUMBER = re.compile(
    r"^((?:[1-9]|1\d|2[0-7])\.\d{1,2}(?:\.\d{1,3}){0,2})(?:\s+((?:[A-Z(\[]|\d+-[A-Za-z]).*))?$"
)
_LEVELS = re.compile(r"(?<![\w(])([PST](?:\s*,\s*[PST])*)(?![\w)])")
# A footnote starts with asterisks and a word; "* P,S,T" is a name's marker before its levels.
_STOP = re.compile(
    r"^\*+\s*\w{2,}|^Medicine Level of|^Healthcare Dosage|^Section \d"
    r"|^\d{1,2}\.\d{1,2}\s*-|^[A-B]\. Section"
)


@dataclass
class NppaRow:
    serial: int
    medicine: str
    dosage_form: str
    unit: str
    ceiling_price: Decimal
    base_notification: str
    page: int


@dataclass
class NlemEntry:
    number: str
    section: str
    medicine: str
    levels: str | None
    formulations: list[str] = field(default_factory=list)
    page: int = 0


def parse_nppa_rows(tables: Iterable[tuple[int, Table]]) -> list[NppaRow]:
    rows = []
    for page, table in tables:
        for cells in table:
            if len(cells) < 7 or not join_cell(cells[0]).isdigit():
                continue
            price = amount(cells[4])
            if price is None:
                continue
            rows.append(
                NppaRow(
                    serial=int(join_cell(cells[0])),
                    medicine=join_cell(cells[1]),
                    dosage_form=join_cell(cells[2]),
                    unit=join_cell(cells[3]),
                    ceiling_price=price,
                    base_notification=f"S.O. {join_cell(cells[5])} dated {join_cell(cells[6])}",
                    page=page,
                )
            )
    return rows


def parse_nlem(pages: Iterable[tuple[int, str]]) -> list[NlemEntry]:
    """Medicines by number. An entry runs from its number to the next number, a heading or a
    footnote. The levels of care are the first standalone P/S/T list; the name is before
    them, the formulations after, one per line, with a line starting in lower case continuing
    the one before."""
    entries: list[NlemEntry] = []
    section = ""
    pending_section = False
    current: list[str] | None = None
    current_meta: tuple[str, int] | None = None

    def close() -> None:
        nonlocal current, current_meta
        if current is not None and current_meta is not None:
            entries.append(_entry(current_meta[0], section, current, current_meta[1]))
        current, current_meta = None, None

    for page, text in pages:
        for line in lines_of(text):
            heading = re.match(r"^Section (\d{1,2})\s*$", line)
            if heading:
                close()
                section, pending_section = heading[1], True
                continue
            if pending_section:
                section, pending_section = f"{section} {line}", False
                continue
            number = _NUMBER.match(line)
            if number:
                close()
                current, current_meta = [number[2] or ""], (number[1], page)
                continue
            if _STOP.search(line):
                close()
                continue
            if current is not None:
                current.append(line)
    close()
    # A numbered line with no levels of care whose number heads deeper numbers is a heading
    # (section 6 numbers its medicines to four levels).
    numbers = [e.number for e in entries]
    return [
        e for e in entries if e.levels or not any(n.startswith(e.number + ".") for n in numbers)
    ]


def _entry(number: str, section: str, lines: list[str], page: int) -> NlemEntry:
    text = " ".join(lines)
    levels = _LEVELS.search(text)
    if levels is None:
        return NlemEntry(number, section, _name(text), None, [], page)
    name = _name(text[: levels.start()])
    after = text[levels.end() :].strip()
    # Rebuild the formulation lines: find where the levels end within the original lines.
    formulations: list[str] = []
    consumed = 0
    for line in lines:
        start, end = consumed, consumed + len(line)
        consumed = end + 1
        if end <= levels.end():
            continue
        piece = line[max(0, levels.end() - start) :].strip()
        if not piece:
            continue
        if formulations and piece[:1].islower():
            formulations[-1] = f"{formulations[-1]} {piece}"
        else:
            formulations.append(piece)
    if not formulations and after:
        formulations = [after]
    return NlemEntry(
        number,
        section,
        name,
        re.sub(r"\s+", "", levels[1]),
        [clean(f) for f in formulations],
        page,
    )


def _name(text: str) -> str:
    return clean(text.replace("*", ""))


async def load_drugs(sessions: async_sessionmaker[AsyncSession], source_dir: Path) -> LoadReport:
    nppa_file = source_file(source_dir, NPPA, "pp.31-51 English table")
    rows = parse_nppa_rows(page_tables(source_path(source_dir, NPPA), NPPA_PAGES))
    nlem_file = source_file(source_dir, NLEM, "pp.4-84 medicine list")
    entries = parse_nlem(page_texts(source_path(source_dir, NLEM), NLEM_PAGES))
    report = LoadReport(system=PRICES.system, version=PRICES.version)

    serials = sorted(r.serial for r in rows)
    report.checks["NPPA serials"] = (
        f"{len(serials)} rows, expected {NPPA_EXPECTED}; missing "
        f"{sorted(set(range(1, NPPA_EXPECTED + 1)) - set(serials))[:10] or 'none'}; repeated "
        f"{len(serials) - len(set(serials))}"
    )
    nlem_names = {e.medicine.lower() for e in entries}
    in_nlem = sum(
        1
        for r in rows
        if any(
            part.strip().lower() in nlem_names for part in re.split(r"\s*\(\w\)\s*\+?", r.medicine)
        )
    )
    report.checks["NPPA medicines named in NLEM 2022 (single-name match)"] = percent(
        in_nlem, len(rows)
    )
    report.checks["NLEM entries with levels of care"] = percent(
        sum(1 for e in entries if e.levels), len(entries)
    )

    sample = Sampler()
    async with sessions.begin() as session:
        await clear_release(session, PRICES, DrugPriceRow)
        await clear_release(session, NLEM_RELEASE, NlemMedicineRow)
        if rows:
            await session.execute(
                insert(DrugPriceRow),
                [
                    {
                        "serial": r.serial,
                        "medicine": r.medicine,
                        "dosage_form": r.dosage_form,
                        "unit": r.unit,
                        "ceiling_price": r.ceiling_price,
                        "notification": f"{NOTIFICATION}; first fixed by {r.base_notification}",
                        "effective_from": PRICES.effective_from,
                        "page": r.page,
                        "source": PRICES.system,
                        "source_version": PRICES.version,
                    }
                    for r in rows
                ],
            )
        if entries:
            await session.execute(
                insert(NlemMedicineRow),
                [
                    {
                        "number": e.number,
                        "section": e.section,
                        "medicine": e.medicine,
                        "levels": e.levels,
                        "formulations": e.formulations,
                        "page": e.page,
                        "source": NLEM_RELEASE.system,
                        "source_version": NLEM_RELEASE.version,
                    }
                    for e in entries
                ],
            )
        for r in rows:
            sample.offer(
                f"NPPA {r.serial} p{r.page} {r.medicine!r} {r.dosage_form!r} per {r.unit!r} "
                f"Rs {r.ceiling_price} ({r.base_notification})"
            )
        nppa_file.rows, nlem_file.rows = len(rows), len(entries)
        record_release(session, PRICES, [nppa_file])
        record_release(session, NLEM_RELEASE, [nlem_file])
        await session.flush()
        await analyze(session, "drug_prices", "nlem_medicines", "kb_source_loads")

    report.files = [nppa_file, nlem_file]
    report.counts = {
        "NPPA formulations": len(rows),
        "NLEM medicines": len(entries),
        "NLEM formulations": sum(len(e.formulations) for e in entries),
    }
    report.sample = sample.rows[:15] + [
        f"NLEM {e.number} p{e.page} {e.medicine} [{e.levels}] {e.formulations[:2]}"
        for e in entries[:: max(1, len(entries) // 5)][:5]
    ]
    return report
