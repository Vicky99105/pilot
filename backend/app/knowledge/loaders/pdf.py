"""What every public reference loader shares: reading PDF pages as text or as tables, page by
page, and recording what was loaded.

Source files are untrusted data. They are only parsed, never executed. Text comes from
pypdfium2; tables, where a source needs cell boundaries, come from pdfplumber. Both are read
one page at a time and each page is closed before the next, so memory stays flat whatever the
page count.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2 as pdfium
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import KbSourceLoadRow
from app.knowledge.loaders.common import SourceFile, SourceMissing, sha256_of

DEFAULT_REFERENCE_DIR = Path(".local/kb_sources")
OPEN_ENDED = date(9999, 12, 31)
# For a source that does not print the date it is in force from.
NOT_STATED = date(1900, 1, 1)

Table = list[list[str | None]]

# pdfium marks a hyphen at a line break with U+FFFE; some files carry soft hyphens.
_HYPHENS = str.maketrans({"\ufffe": "-", "\u00ad": "-", "\u2013": "-", "\u2011": "-"})


@dataclass(frozen=True)
class ReferenceRelease:
    """The version a reference loader writes, with the dates it is in force."""

    system: str  # the loader name, e.g. "cghs"; kb_source_loads.system
    version: str
    effective_from: date
    effective_to: date = OPEN_ENDED


def clean(text: str | None) -> str:
    """One line of text: hyphen markers fixed, whitespace collapsed."""
    if not text:
        return ""
    return " ".join(text.translate(_HYPHENS).split())


def source_path(source_dir: Path, relative: str) -> Path:
    path = source_dir / relative
    if not path.is_file():
        raise SourceMissing(f"{relative} not found in {source_dir}")
    return path


def source_file(source_dir: Path, relative: str, pages: str = "") -> SourceFile:
    """The file's record for the load report and kb_source_loads. member holds the pages
    read, e.g. "pp.20-25"."""
    return SourceFile(
        file_name=relative, member=pages, sha256=sha256_of(source_path(source_dir, relative))
    )


def page_texts(path: Path, pages: Iterable[int]) -> Iterator[tuple[int, str]]:
    """(page number, text) for each 1-based page asked for that the file has."""
    document = pdfium.PdfDocument(str(path))
    try:
        count = len(document)
        for number in pages:
            if not 1 <= number <= count:
                continue
            page = document[number - 1]
            textpage = page.get_textpage()
            try:
                yield number, str(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
    finally:
        document.close()


def page_count(path: Path) -> int:
    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def page_tables(path: Path, pages: Iterable[int]) -> Iterator[tuple[int, Table]]:
    """(page number, table) for each table pdfplumber finds on each page asked for."""
    with pdfplumber.open(str(path)) as pdf:
        count = len(pdf.pages)
        for number in pages:
            if not 1 <= number <= count:
                continue
            page = pdf.pages[number - 1]
            try:
                for table in page.extract_tables():
                    yield number, table
            finally:
                page.close()


def lines_of(text: str) -> list[str]:
    """The non-empty lines of a page, each cleaned."""
    return [line for line in (clean(raw) for raw in text.splitlines()) if line]


def norm_key(text: str) -> str:
    """Letters and digits only, lower case: for comparing labels across copies of a list
    whose text extraction drops or adds spaces."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


async def clear_release(session: AsyncSession, release: ReferenceRelease, *models: Any) -> None:
    """Delete the release's earlier rows in each model (by source and source_version) and its
    earlier load records, so a re-run replaces them."""
    for model in models:
        await session.execute(
            delete(model).where(
                model.source == release.system, model.source_version == release.version
            )
        )
    await session.execute(
        delete(KbSourceLoadRow).where(
            KbSourceLoadRow.system == release.system,
            KbSourceLoadRow.version == release.version,
        )
    )


def record_release(
    session: AsyncSession, release: ReferenceRelease, files: list[SourceFile]
) -> None:
    session.add_all(
        KbSourceLoadRow(
            system=release.system,
            version=release.version,
            effective_from=release.effective_from,
            effective_to=release.effective_to,
            file_name=f.file_name,
            member=f.member,
            sha256=f.sha256,
            rows=f.rows,
        )
        for f in files
    )


def amount(cell: str | None) -> Decimal | None:
    """The first number in a cell, with its thousands commas and stray spaces dropped
    ("6 900" -> 6900, "-\n87000" -> 87000); None when the cell holds none ("NA")."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", re.sub(r"(?<=\d) (?=\d)", "", cell or ""))
    return Decimal(match[0].replace(",", "")) if match else None


def search_text(*parts: str | None) -> str:
    """Lower-case text a name lookup searches: the parts joined, a part already contained in
    an earlier one left out."""
    out: list[str] = []
    for part in parts:
        text = clean(part).lower()
        if text and not any(text in earlier for earlier in out):
            out.append(text)
    return " ".join(out)


def percent(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.1f}%)" if whole else "0/0"
