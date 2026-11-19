"""PM-JAY standard treatment guidelines (STGs), from the Uttarakhand SHA mirror of the NHA sets.

Two sources:
- The STG compilation (592 pages): one table row per procedure and required document, with
  the package, procedure name, HBP 2.0 code, amount, LOS and treating doctor's qualification.
  Rows are grouped per procedure into four sections: documents at pre-authorisation,
  documents at claim, LOS and the doctor's qualification. On some pages a long package name
  overflows into the next cells and pdfplumber interleaves the two texts; such a row's code is
  recovered from the page text (pypdfium2) by its serial number, and its name is the page
  text before the code.
- The 50 STG PDFs (PPD/CPD processing versions): page 1 names the HBP 2.0 codes the guideline
  covers; the loader keeps four sections as short lists: clinical key pointers, pre-auth
  checks, claim checks and TMS rules.

The STGs carry HBP 2.0 codes. Each code is matched to HBP 2022 by procedure name against the
loaded PM-JAY package master (run the hbp loader first). A code that still exists with a
matching name keeps it; otherwise the best name match at or above MATCH_SCORE wins; below that,
the guideline is kept under its old code and reported.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz, process
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import GuidelineSectionRow, LosNormRow, PackageMasterRow
from app.knowledge.loaders.common import LoadReport, Sampler, analyze
from app.knowledge.loaders.hbp import RELEASE as HBP_RELEASE
from app.knowledge.loaders.hbp import SCHEME as HBP_SCHEME
from app.knowledge.loaders.hbp import join_cell, los_days
from app.knowledge.loaders.pdf import (
    ReferenceRelease,
    Table,
    clean,
    clear_release,
    lines_of,
    norm_key,
    page_count,
    page_tables,
    page_texts,
    percent,
    record_release,
    source_file,
    source_path,
)

COMPILATION = "s8_stg_compilation_uk/STG_compilation_of_packages.pdf"
STG_DIR = "s8_stg_pdfs"
RELEASE = ReferenceRelease("stg", "NHA-STG-sets-1-22", date(2020, 6, 1))
MATCH_SCORE = 88.0  # token_set_ratio at or above this is a name match
KEEP_SCORE = 70.0  # a code that still exists keeps it at or above this

_CODE = re.compile(r"^[A-Z]{2}\d{3,4}[A-Z]{1,2}$")
_CODE_IN_TEXT = re.compile(r"\b([A-Z]{2}\d{3,4}[A-Z]{1,2})\b")
_SERIAL = re.compile(r"^(\d{1,4}) (?:Set|SET|set) ?\d+\b(.*)$")
_ITEM = re.compile(r"^(?:[a-z]|[ivx]{1,4})\s*[.)]\s+(\S.*)$|^[•▪-]\s*(\S.*)$")
_ANSWER = re.compile(r"\s*[\u2013-]?\s*(?:Yes|No)\.?\s*$")
_NOT_ITEMS = re.compile(r"^Till the time|^References|^National Health Authority", re.I)
_SECTIONS = {
    "clinical_pointers": re.compile(r"^1\.\d\s+Clinical key pointers", re.I),
    "preauth_checks": re.compile(r"^(?:2\.2\.1\b|i\.\s*At the time of pre)", re.I),
    "claim_checks": re.compile(r"^(?:2\.2\.2\b|ii\.\s*At the time of claim)", re.I),
    "tms_rules": re.compile(r"^3\.2\b"),
}
_SECTION_END = re.compile(r"^(?:\d\.\d(?:\.\d)?\s|PART\s|References)", re.I)


@dataclass
class Procedure:
    old_code: str
    name: str
    package_name: str
    los_text: str
    qualification: str
    preauth_documents: list[str] = field(default_factory=list)
    claim_documents: list[str] = field(default_factory=list)
    page: int = 0
    recovered: bool = False


@dataclass
class Section:
    items: list[str]
    page: int


@dataclass
class StgDocument:
    title: str
    codes: list[str]
    sections: dict[str, Section]


def _document(cell: str | None) -> str:
    text = join_cell(cell)
    return re.sub(r"^(?:[a-z]|[ivx]{1,4})\s*[.)]\s*", "", text).strip()


_GARBLED = re.compile(r"[a-z][A-Z]|[A-Za-z]\d[A-Za-z]")
_LOS_AFTER_CODE = re.compile(
    r"^\s*(?:Rs\.?\s*)?[\d,]+(?:/-)?(?:\s*\+[^0-9]*?)?\s+(.{1,40}?)\s+(?:Essential|Desirable|MBBS|"
    r"M\.?S\b|M\.?D\b|M\.?Ch|DNB|:)",
)


def garbled(text: str) -> bool:
    """Two texts interleaved character by character ("uDmean t-o walivrienoglar") show
    lower-upper or letter-digit-letter runs inside words, which real names rarely do."""
    return len(_GARBLED.findall(text)) >= 2


def serial_codes(pages: Iterable[tuple[int, str]]) -> dict[int, tuple[str, str, str]]:
    """serial -> (code, text before the code, text after it), from the compilation's page
    text, where a row's cells come out in order even when the table cells interleave."""
    blocks: dict[int, str] = {}
    current: int | None = None
    for _, text in pages:
        for line in lines_of(text):
            match = _SERIAL.match(line)
            if match:
                current = int(match[1])
                blocks[current] = match[2]
            elif current is not None:
                blocks[current] += " " + line
    found = {}
    for serial, block in blocks.items():
        code = _CODE_IN_TEXT.search(block)
        if code:
            found[serial] = (code[1], clean(block[: code.start()]), clean(block[code.end() :]))
    return found


def parse_compilation(
    tables: Iterable[tuple[int, Table]],
    recovered: dict[int, tuple[str, str, str]],
    page_keys: dict[int, str] | None = None,
) -> dict[str, Procedure]:
    """Procedures by HBP 2.0 code. A row whose code is not a code, or whose name or LOS is
    garbled, takes them from the page text by its serial number. With page_keys (each page's
    text as norm_key), a cell is garbled when its letters do not appear in its page's text;
    without, a pattern test decides."""

    def is_garbled(text: str, page: int) -> bool:
        if page_keys is None:
            return garbled(text)
        key = norm_key(text)
        return bool(key) and key not in page_keys.get(page, "") + page_keys.get(page + 1, "")

    procedures: dict[str, Procedure] = {}
    for page, table in tables:
        for cells in table:
            if len(cells) < 11 or not join_cell(cells[0]).isdigit():
                continue
            code = re.sub(r"\s", "", cells[5] or "")
            was_recovered = False
            name, los = join_cell(cells[4]), join_cell(cells[7])
            serial = int(join_cell(cells[0]))
            if not _CODE.match(code) or is_garbled(name, page) or is_garbled(los, page):
                if serial not in recovered:
                    continue
                code, text, after = recovered[serial]
                specialty = join_cell(cells[2])
                name = text[len(specialty) :].strip() if text.startswith(specialty) else text
                los_match = _LOS_AFTER_CODE.match(after)
                los = los_match[1] if los_match else ""
                was_recovered = True
            procedure = procedures.get(code)
            if procedure is None:
                procedure = Procedure(
                    old_code=code,
                    name=name,
                    package_name=join_cell(cells[3]),
                    los_text=los,
                    qualification=join_cell(cells[8]),
                    page=page,
                    recovered=was_recovered,
                )
                procedures[code] = procedure
            for document, target in (
                (_document(cells[9]), procedure.preauth_documents),
                (_document(cells[10]), procedure.claim_documents),
            ):
                if document and document not in target:
                    target.append(document)
    return procedures


def parse_stg_pdf(pages: Iterable[tuple[int, str]]) -> StgDocument:
    title = ""
    codes: list[str] = []
    sections: dict[str, Section] = {}
    current: str | None = None
    for page, text in pages:
        lines = lines_of(text)
        if page == 1:
            title = lines[2] if len(lines) > 2 else ""
            for line in lines:
                if _SECTIONS["clinical_pointers"].match(line):
                    break
                for code in _CODE_IN_TEXT.findall(line):
                    if code not in codes:
                        codes.append(code)
        for line in lines:
            started = next((name for name, rx in _SECTIONS.items() if rx.match(line)), None)
            if started:
                current = started
                sections[current] = Section([], page)
                continue
            if current is None:
                continue
            if _SECTION_END.match(line) or _NOT_ITEMS.match(line):
                current = None
                continue
            item = _ITEM.match(line)
            items = sections[current].items
            if item:
                items.append(item[1] or item[2])
            elif items:
                items[-1] = f"{items[-1]} {line}"
    for section in sections.values():
        section.items = [_ANSWER.sub("", clean(i)).strip() for i in section.items]
    return StgDocument(title=title, codes=codes, sections=sections)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def match_codes(
    old: dict[str, list[str]], hbp: list[tuple[str, str, str]]
) -> dict[str, tuple[str | None, float]]:
    """old code -> (HBP 2022 code or None, score). hbp rows are (code, package name, name)."""
    choices = {code: _norm(f"{package} {name}") for code, package, name in hbp}
    names = {code: _norm(name) for code, _, name in hbp}
    result: dict[str, tuple[str | None, float]] = {}
    for code, labels in old.items():
        best: tuple[str | None, float] = (None, 0.0)
        for label in (_norm(x) for x in labels if x):
            if code in choices:
                kept = max(
                    fuzz.token_set_ratio(label, choices[code]),
                    fuzz.token_set_ratio(label, names[code]),
                )
                if kept >= KEEP_SCORE and kept > best[1]:
                    best = (code, kept)
            found = process.extractOne(label, choices, scorer=fuzz.token_set_ratio)
            if found and found[1] > best[1] and found[1] >= MATCH_SCORE:
                best = (found[2], found[1])
        if best[0] is not None and best[0] == code:
            result[code] = best
        elif best[0] is not None and best[1] >= MATCH_SCORE:
            result[code] = best
        else:
            result[code] = (None, best[1])
    return result


async def load_stg(sessions: async_sessionmaker[AsyncSession], source_dir: Path) -> LoadReport:
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    async with sessions() as session:
        hbp = [
            (r.procedure_code, r.package_name or "", r.name)
            for r in await session.execute(
                select(PackageMasterRow.procedure_code, PackageMasterRow.package_name,
                       PackageMasterRow.name).where(
                    PackageMasterRow.scheme == HBP_SCHEME,
                    PackageMasterRow.version == HBP_RELEASE.version,
                )
            )
        ]  # fmt: skip
    if not hbp:
        raise ValueError("load the HBP 2022 package master first: python -m app.knowledge.load hbp")
    hbp = list({code: (code, p, n) for code, p, n in hbp}.values())

    path = source_path(source_dir, COMPILATION)
    pages = page_count(path)
    compilation = source_file(source_dir, COMPILATION, f"pp.1-{pages}")
    texts = list(page_texts(path, range(1, pages + 1)))
    recovered = serial_codes(texts)
    page_keys = {page: norm_key(text) for page, text in texts}
    procedures = parse_compilation(page_tables(path, range(1, pages + 1)), recovered, page_keys)

    stg_files = sorted((source_dir / STG_DIR).glob("*.pdf"))
    documents = []
    files = [compilation]
    for stg_path in stg_files:
        relative = f"{STG_DIR}/{stg_path.name}"
        record = source_file(source_dir, relative, "all pages")
        document = parse_stg_pdf(page_texts(stg_path, range(1, page_count(stg_path) + 1)))
        documents.append((relative, record, document))
        files.append(record)

    names: dict[str, list[str]] = {
        code: [p.name, f"{p.package_name} {p.name}"] for code, p in procedures.items()
    }
    for _, _, document in documents:
        for code in document.codes:
            names.setdefault(code, []).append(document.title)
    matched = match_codes(names, hbp)

    sample = Sampler()
    rows: list[dict[str, object]] = []
    los_rows: list[dict[str, object]] = []
    source = {"source": RELEASE.system, "source_version": RELEASE.version}
    for code, procedure in procedures.items():
        new_code, score = matched[code]
        common = {
            "package_code": new_code or code,
            "old_code": code,
            "procedure_name": procedure.name,
            "match_score": score if new_code else None,
            "file_name": COMPILATION,
            "page": procedure.page,
            **source,
        }
        for section, items in (
            ("preauth_documents", procedure.preauth_documents),
            ("claim_documents", procedure.claim_documents),
            ("alos", [procedure.los_text] if procedure.los_text else []),
            ("doctor_qualification", [procedure.qualification] if procedure.qualification else []),
        ):
            if items:
                rows.append({**common, "section": section, "items": items})
        if procedure.los_text:
            los_rows.append(
                {
                    "scheme": HBP_SCHEME,
                    "version": RELEASE.version,
                    "package_code": new_code or code,
                    "name": procedure.name,
                    "specialty": None,
                    "los_days": 0
                    if "day" in procedure.los_text.lower() and "care" in procedure.los_text.lower()
                    else los_days(procedure.los_text),
                    "los_text": procedure.los_text,
                    "page": procedure.page,
                    **source,
                }
            )
        sample.offer(
            f"{code} -> {new_code or '(unmatched)'} p{procedure.page} {procedure.name[:50]!r} "
            f"LOS {procedure.los_text!r} preauth {len(procedure.preauth_documents)} claim "
            f"{len(procedure.claim_documents)}"
        )
    pdf_sections = 0
    for relative, record, document in documents:
        record.rows = 0
        for code in document.codes:
            new_code, score = matched.get(code, (None, 0.0))
            for section, content in document.sections.items():
                if not content.items:
                    continue
                rows.append(
                    {
                        "package_code": new_code or code,
                        "old_code": code,
                        "procedure_name": document.title,
                        "section": section,
                        "items": content.items,
                        "match_score": score if new_code else None,
                        "file_name": relative,
                        "page": content.page,
                        **source,
                    }
                )
                record.rows += 1
                pdf_sections += 1

    async with sessions.begin() as session:
        await clear_release(session, RELEASE, GuidelineSectionRow, LosNormRow)
        for start in range(0, len(rows), 2000):
            await session.execute(insert(GuidelineSectionRow), rows[start : start + 2000])
        if los_rows:
            await session.execute(insert(LosNormRow), los_rows)
        compilation.rows = len(procedures)
        record_release(session, RELEASE, files)
        await session.flush()
        await analyze(session, "guideline_sections", "los_norms", "kb_source_loads")

    all_codes = set(matched)
    hits = [c for c in all_codes if matched[c][0]]
    report.files = files
    report.counts = {
        "compilation procedures": len(procedures),
        "compilation rows recovered from page text": sum(
            1 for p in procedures.values() if p.recovered
        ),
        "STG PDFs": len(documents),
        "STG PDF codes": len({c for _, _, d in documents for c in d.codes}),
        "guideline sections": len(rows),
        "of which from STG PDFs": pdf_sections,
        "LOS norms": len(los_rows),
    }  # fmt: skip
    report.checks["HBP 2.0 codes matched to HBP 2022"] = percent(len(hits), len(all_codes))
    report.checks["matched codes unchanged in HBP 2022"] = percent(
        sum(1 for c in hits if matched[c][0] == c), len(hits)
    )
    report.checks["STG PDFs with no code on page 1"] = (
        ", ".join(r for r, _, d in documents if not d.codes) or "none"
    )
    report.checks["STG PDFs missing a section"] = (
        "; ".join(
            f"{r.split('/')[-1][:40]}: {sorted(set(_SECTIONS) - set(d.sections))}"
            for r, _, d in documents
            if set(_SECTIONS) - set(d.sections)
        )
        or "none"
    )
    report.sample = sample.rows
    return report
