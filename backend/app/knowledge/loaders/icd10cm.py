"""ICD-10-CM loader: the tabular list, the order file, and the four alphabetic indexes
(diseases, external causes, the drug table and the neoplasm table).

The order file is the list of codes: every code with its long title and whether it is valid
for submission, including the 7th character codes that the tabular XML only describes. The
tabular XML adds each code's notes, its chapter and section, and the 7th character
definitions, which every code below the defining code inherits.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import IcdIndexRow, IcdTabularRow, KbSourceLoadRow
from app.knowledge.loaders.common import (
    FY2027,
    SOURCE,
    Archives,
    LoadReport,
    Release,
    Sampler,
    analyze,
    dotted,
    insert_batches,
    parse_order,
    record_loads,
    stream_elements,
    text_of,
)
from app.services.knowledge_base.contract import ICD10CM

IndexName = Literal["diseases", "external_causes", "drugs", "neoplasms"]

TABLES_ZIP = "2027-code-tables-tabular-index.zip"
ORDER_ZIP = "2027-code-descriptions-tabular-order.zip"
TABULAR_XML = "Table and Index/icd10cm_tabular_2027.xml"
ORDER_TXT = "Code Descriptions/icd10cm_order_2027.txt"
INDEX_XML: dict[IndexName, str] = {
    "diseases": "Table and Index/icd10cm_index_2027.xml",
    "external_causes": "Table and Index/icd10cm_eindex_2027.xml",
    "drugs": "Table and Index/icd10cm_drug_2027.xml",
    "neoplasms": "Table and Index/icd10cm_neoplasm_2027.xml",
}

# Tabular note elements and the key each is stored under.
NOTE_KEYS = {
    "inclusionTerm": "inclusion_terms",
    "includes": "includes",
    "excludes1": "excludes1",
    "excludes2": "excludes2",
    "codeFirst": "code_first",
    "useAdditionalCode": "use_additional",
    "codeAlso": "code_also",
    "notes": "notes",
    "sevenChrNote": "seven_chr_note",
}
_FRAMES = ("chapter", "section", "diag")
_NO_CODE = {"-", "--"}


# --- Tabular -------------------------------------------------------------------------------


@dataclass
class TabularEntry:
    code: str  # dotted, e.g. "K35.80"
    chapter: str
    section: str
    notes: dict[str, list[str]] = field(default_factory=dict)
    seven_chr_def: dict[str, str] = field(default_factory=dict)  # own or inherited


@dataclass
class _Frame:
    tag: str
    name: str = ""
    desc: str = ""
    notes: dict[str, list[str]] = field(default_factory=dict)
    seven_chr_def: dict[str, str] = field(default_factory=dict)


def parse_tabular(stream: IO[bytes]) -> Iterator[TabularEntry]:
    """Every diag element of the tabular list, with its own notes."""
    stack: list[_Frame] = []
    for event, element in stream_elements(stream):
        tag = element.tag
        if event == "start":
            if tag in _FRAMES:
                stack.append(_Frame(tag))
            continue
        if not stack:
            continue
        top = stack[-1]
        if tag == "name" and not top.name:
            top.name = text_of(element)
        elif tag == "desc" and not top.desc:
            top.desc = text_of(element)
        elif tag in NOTE_KEYS:
            notes = [text_of(n) for n in element.findall("note")]
            top.notes.setdefault(NOTE_KEYS[tag], []).extend(n for n in notes if n)
        elif tag == "sevenChrDef":
            top.seven_chr_def = {
                (ext.get("char") or "").strip(): text_of(ext)
                for ext in element.findall("extension")
            }
        elif tag in _FRAMES:
            frame = stack.pop()
            if tag == "diag":
                inherited: dict[str, str] = {}
                for outer in [*stack, frame]:
                    if outer.seven_chr_def:
                        inherited = outer.seven_chr_def
                chapter = next((f for f in stack if f.tag == "chapter"), None)
                section = next((f for f in stack if f.tag == "section"), None)
                yield TabularEntry(
                    code=frame.name,
                    chapter=f"{chapter.name}. {chapter.desc}" if chapter else "",
                    section=section.desc if section else "",
                    notes=frame.notes,
                    seven_chr_def=inherited,
                )
            element.clear()


# --- Index ---------------------------------------------------------------------------------


@dataclass
class IndexEntry:
    index_name: IndexName
    term: str  # the main term
    path: str  # main term and sub-terms joined by " > "
    search_text: str  # the path plus every nonessential modifier along it
    code: str | None = None  # as written, e.g. "K35.89-"
    table_column: str | None = None  # drug and neoplasm tables: the column heading
    manifestation: str | None = None
    see: str | None = None
    see_also: str | None = None


@dataclass
class _Term:
    title: str | None = None
    modifiers: list[str] = field(default_factory=list)
    code: str | None = None
    manifestation: str | None = None
    see: str | None = None
    see_also: str | None = None
    cells: list[tuple[int, str]] = field(default_factory=list)


def parse_index(stream: IO[bytes], index_name: IndexName) -> Iterator[IndexEntry]:
    """Every index term that gives a code, a table cell or a cross-reference, with its full
    path. Sub-terms that only group others ("with") are part of the paths below them."""
    headings: dict[int, str] = {}
    stack: list[_Term] = []
    for event, element in stream_elements(stream):
        tag = element.tag
        if event == "start":
            if tag in ("mainTerm", "term"):
                stack.append(_Term())
            continue
        if tag == "head":
            headings[int(element.get("col") or 0)] = text_of(element)
            continue
        if not stack:
            continue
        top = stack[-1]
        if tag in ("mainTerm", "term"):
            yield from _index_entries(index_name, stack, headings)
            stack.pop()
            element.clear()
            continue
        if tag == "title":
            if top.title is None:
                top.title = text_of(element, skip=("nemod",))
                top.modifiers = [text_of(n) for n in element.iter("nemod")]
            continue
        value = text_of(element)
        if tag == "code":
            top.code = value or None
        elif tag == "manif":
            top.manifestation = value or None
        elif tag == "see":
            top.see = value or None
        elif tag == "seeAlso":
            top.see_also = value or None
        elif tag == "seecat":
            top.see = top.see or f"category {value}"
        elif tag == "subcat":
            top.see = top.see or f"subcategory {value}"
        elif tag == "cell":
            top.cells.append((int(element.get("col") or 0), value))


def _index_entries(
    index_name: IndexName, stack: list[_Term], headings: dict[int, str]
) -> Iterator[IndexEntry]:
    term = stack[-1]
    path = " > ".join(t.title or "" for t in stack)
    modifiers = [m for t in stack for m in t.modifiers]
    base: dict[str, Any] = {
        "index_name": index_name,
        "term": stack[0].title or "",
        "path": path,
        "search_text": " ".join([path, *modifiers]),
    }
    cells = [(col, code) for col, code in term.cells if code and code not in _NO_CODE]
    if cells:
        for col, code in cells:
            yield IndexEntry(**base, code=code, table_column=headings.get(col))
        if term.see or term.see_also:
            yield IndexEntry(**base, see=term.see, see_also=term.see_also)
    elif term.code or term.see or term.see_also or term.manifestation:
        yield IndexEntry(
            **base,
            code=term.code,
            manifestation=term.manifestation,
            see=term.see,
            see_also=term.see_also,
        )


# --- Load ----------------------------------------------------------------------------------


def _parent(code: str, known: set[str]) -> str | None:
    for end in range(len(code) - 1, 2, -1):
        if code[:end] in known:
            return code[:end]
    return None


async def load_icd10cm(
    sessions: async_sessionmaker[AsyncSession],
    source_dir: Path,
    release: Release = FY2027,
) -> LoadReport:
    """Replace the release's ICD-10-CM rows in one transaction, and remove the fixture."""
    archives = Archives(source_dir)
    archives.check(TABLES_ZIP, ORDER_ZIP)
    report = LoadReport(system=ICD10CM, version=release.version)

    tabular_file = archives.source_file(TABLES_ZIP, TABULAR_XML)
    with archives.open(TABLES_ZIP, TABULAR_XML) as stream:
        entries = {e.code: e for e in parse_tabular(stream)}
    tabular_file.rows = len(entries)
    order_file = archives.source_file(ORDER_ZIP, ORDER_TXT)
    with archives.open(ORDER_ZIP, ORDER_TXT) as stream:
        order = list(parse_order(stream))
    order_file.rows = len(order)
    report.files += [tabular_file, order_file]

    known = {line.code for line in order}
    orphans = 0
    tabular_sample, index_sample = Sampler(10), Sampler(10)

    def tabular_rows() -> Iterator[dict[str, Any]]:
        nonlocal orphans
        for line in order:
            code = dotted(line.code)
            entry = entries.get(code)
            ancestor = entry
            raw = line.code
            while ancestor is None and len(raw) > 3:
                raw = raw[:-1]
                ancestor = entries.get(dotted(raw))
            if ancestor is None:
                orphans += 1
            notes: dict[str, Any] = dict(entry.notes) if entry else {}
            if ancestor and ancestor.seven_chr_def:
                notes["seven_chr_def"] = ancestor.seven_chr_def
            parent = _parent(line.code, known)
            row = {
                "system": ICD10CM,
                "version": release.version,
                "code": code,
                "title": line.title,
                "billable": line.valid,
                "notes": notes,
                "parent": dotted(parent) if parent else None,
                "chapter": ancestor.chapter if ancestor else None,
                "section": ancestor.section if ancestor else None,
                "source": SOURCE,
                "source_version": release.version,
            }
            tabular_sample.offer(
                f"tabular {code} billable={line.valid} parent={row['parent']} "
                f"notes={sorted(notes)} {line.title!r}"
            )
            yield row

    async with sessions.begin() as session:
        await _clear(session, release)
        count = await insert_batches(session, IcdTabularRow.__table__, tabular_rows())  # type: ignore[arg-type]
        report.counts["tabular codes"] = count
        report.counts["billable codes"] = sum(1 for line in order if line.valid)
        for name, member in INDEX_XML.items():
            index_file = archives.source_file(TABLES_ZIP, member)
            with archives.open(TABLES_ZIP, member) as stream:
                rows = (_index_row(e, release, index_sample) for e in parse_index(stream, name))
                index_file.rows = await insert_batches(session, IcdIndexRow.__table__, rows)  # type: ignore[arg-type]
            report.counts[f"index: {name}"] = index_file.rows
            report.files.append(index_file)
        record_loads(session, ICD10CM, release, report.files)
        await session.flush()
        await analyze(session, "icd_tabular", "icd_index", "kb_source_loads")

    report.checks = {
        "tabular codes missing from the order file": str(
            sum(1 for code in entries if code.replace(".", "") not in known)
        ),
        "order codes with no tabular entry or ancestor": str(orphans),
    }
    report.sample = tabular_sample.rows + index_sample.rows
    return report


def _index_row(entry: IndexEntry, release: Release, sample: Sampler) -> dict[str, Any]:
    sample.offer(
        f"index {entry.index_name} {entry.path!r} -> "
        + (f"{entry.code}" if entry.code else f"see {entry.see or entry.see_also!r}")
        + (f" [{entry.table_column}]" if entry.table_column else "")
    )
    return {
        "system": ICD10CM,
        "version": release.version,
        "index_name": entry.index_name,
        "term": entry.term,
        "path": entry.path,
        "search_text": entry.search_text,
        "table_column": entry.table_column,
        "code": entry.code,
        "manifestation": entry.manifestation,
        "see": entry.see,
        "see_also": entry.see_also,
        "source": SOURCE,
        "source_version": release.version,
    }


async def _clear(session: AsyncSession, release: Release) -> None:
    """Remove this release's rows and the hand-made fixture rows."""
    for model in (IcdTabularRow, IcdIndexRow):
        await session.execute(
            delete(model).where(
                model.system == ICD10CM,
                or_(model.version == release.version, model.source == "fixture"),
            )
        )
    await session.execute(
        delete(KbSourceLoadRow).where(
            KbSourceLoadRow.system == ICD10CM, KbSourceLoadRow.version == release.version
        )
    )
