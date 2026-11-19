"""What every ICD loader shares: the release being loaded, safe streaming of zip members,
the order file layout, batched inserts and the load report.

Source files are untrusted data. XML is parsed with defusedxml (no DTDs, no entities, no
external references) and streamed, so memory stays flat whatever the file size.
"""

import hashlib
import random
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import IO, Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import iterparse
from sqlalchemy import Table, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import KbSourceLoadRow

BATCH = 2000
SAMPLE_SIZE = 20
SOURCE = "cms"  # every row loaded from a CMS release
DEFAULT_SOURCE_DIR = Path(".local/kb_sources/icd-2027")


@dataclass(frozen=True)
class Release:
    version: str
    effective_from: date
    effective_to: date


FY2027 = Release("FY2027", date(2026, 10, 1), date(2027, 9, 30))


class SourceMissing(FileNotFoundError):
    """A source archive or a member inside it is not where the loader expects it."""


@dataclass
class SourceFile:
    file_name: str  # the archive
    member: str  # the file inside it
    sha256: str  # of the archive
    rows: int = 0


@dataclass
class LoadReport:
    system: str
    version: str
    files: list[SourceFile] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)
    sample: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"Loaded {self.system} {self.version}"]
        out += [
            f"  {f.file_name} :: {f.member} sha256={f.sha256} rows={f.rows}" for f in self.files
        ]
        out += [f"  {name}: {count}" for name, count in self.counts.items()]
        out += [f"  check {name}: {value}" for name, value in self.checks.items()]
        out += [f"Spot check ({len(self.sample)} rows):"]
        out += [f"  {row}" for row in self.sample]
        return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Archives:
    """The source archives of one release, opened by name. Hashes each archive once."""

    def __init__(self, source_dir: Path) -> None:
        self._dir = source_dir
        self._hashes: dict[str, str] = {}

    def check(self, *names: str) -> None:
        """Fail before any write when an archive is missing."""
        for name in names:
            if not (self._dir / name).is_file():
                raise SourceMissing(f"{name} not found in {self._dir}")

    def source_file(self, name: str, member: str) -> SourceFile:
        if name not in self._hashes:
            self._hashes[name] = sha256_of(self._dir / name)
        return SourceFile(file_name=name, member=member, sha256=self._hashes[name])

    @contextmanager
    def open(self, name: str, member: str) -> Iterator[IO[bytes]]:
        path = self._dir / name
        if not path.is_file():
            raise SourceMissing(f"{name} not found in {self._dir}")
        with zipfile.ZipFile(path) as archive:
            if member not in archive.namelist():
                raise SourceMissing(f"{member} not found in {name}")
            with archive.open(member) as stream:
                yield stream


def stream_elements(stream: IO[bytes]) -> Iterator[tuple[str, Element]]:
    """("start" | "end", element) events, parsed safely."""
    yield from iterparse(stream, events=("start", "end"), forbid_dtd=True)


def text_of(element: Element | None, skip: tuple[str, ...] = ()) -> str:
    """The element's text with nested markup flattened and whitespace collapsed. Children
    named in skip are left out, but the text after them is kept."""
    if element is None:
        return ""
    parts = [element.text or ""]
    for child in element:
        if child.tag not in skip:
            parts.append(text_of(child))
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


@dataclass(frozen=True)
class OrderLine:
    code: str  # without the dot, e.g. "K3580" or "0DTJ4ZZ"
    valid: bool  # 1 in the file: a code valid for submission
    title: str  # the long title


def parse_order(stream: IO[bytes]) -> Iterator[OrderLine]:
    """The CMS order file: order number (5), code (7), valid flag (1), short title (60), long
    title, each followed by one space."""
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line.strip():
            continue
        yield OrderLine(code=line[6:13].strip(), valid=line[14] == "1", title=line[77:].strip())


def dotted(code: str) -> str:
    """ICD-10-CM codes carry a dot after the third character."""
    return code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"


async def insert_batches(
    session: AsyncSession, table: Table, rows: Iterable[dict[str, Any]]
) -> int:
    count = 0
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            await session.execute(insert(table), batch)
            count += len(batch)
            batch = []
    if batch:
        await session.execute(insert(table), batch)
        count += len(batch)
    return count


def record_loads(
    session: AsyncSession, system: str, release: Release, files: list[SourceFile]
) -> None:
    session.add_all(
        KbSourceLoadRow(
            system=system,
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


class Sampler:
    """A fixed-size uniform sample of a stream (reservoir sampling), seeded so a re-run
    prints the same rows."""

    def __init__(self, size: int = SAMPLE_SIZE, seed: int = 2027) -> None:
        self._size = size
        self._seen = 0
        self._random = random.Random(seed)
        self.rows: list[str] = []

    def offer(self, row: str) -> None:
        self._seen += 1
        if len(self.rows) < self._size:
            self.rows.append(row)
            return
        slot = self._random.randrange(self._seen)
        if slot < self._size:
            self.rows[slot] = row


async def analyze(session: AsyncSession, *tables: str) -> None:
    """Refresh the planner statistics after a bulk load (Postgres only), so lookups use the
    trigram and btree indexes from the first query."""
    if session.get_bind().dialect.name != "postgresql":
        return
    for table in tables:
        await session.execute(text(f"ANALYZE {table}"))
