"""ICD-10-PCS loader: the tables (rows of valid axis combinations), the definitions (root
operations, body parts, approaches, devices) and the order file (code titles).

The tables are the source of truth for which codes are valid. The loader checks that the
codes the tables make and the valid codes of the order file are the same set.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import IO, Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    IcdTabularRow,
    KbSourceLoadRow,
    PcsDefinitionRow,
    PcsRowRow,
    PcsTableRow,
)
from app.knowledge.loaders.common import (
    FY2027,
    SOURCE,
    Archives,
    LoadReport,
    Release,
    Sampler,
    analyze,
    insert_batches,
    parse_order,
    record_loads,
    stream_elements,
    text_of,
)
from app.services.knowledge_base.contract import ICD10PCS

TABLES_ZIP = "2027-icd-10-pcs-code-tables-index.zip"
ORDER_ZIP = "2027-icd-10-pcs-order-file-long-abbreviated-titles.zip"
TABLES_XML = "icd10pcs_tables_2027.xml"
DEFINITIONS_XML = "icd10pcs_definitions_2027.xml"
ORDER_TXT = "icd10pcs_order_2027.txt"

Labels = list[tuple[str, str]]  # (character, label)


@dataclass
class PcsRow:
    axes: list[tuple[str, Labels]]  # positions 4 to 7: (axis title, values)
    codes: int  # as the file states it

    def expand(self, table: str) -> Iterator[str]:
        for chars in product(*([c for c, _ in labels] for _, labels in self.axes)):
            yield table + "".join(chars)


@dataclass
class PcsTable:
    code: str  # the first three characters
    axes: list[tuple[str, str, str]]  # positions 1 to 3: (axis title, character, label)
    operation_definition: str | None
    rows: list[PcsRow]


@dataclass
class PcsDefinition:
    section: str
    pos: int
    axis: str
    label: str
    definition: str | None
    explanation: str | None
    includes: list[str]


def _axis(element: Any) -> tuple[int, str, Labels, str | None]:
    labels = [((lab.get("code") or "").strip(), text_of(lab)) for lab in element.findall("label")]
    definition = element.find("definition")
    return (
        int(element.get("pos") or 0),
        text_of(element.find("title")),
        labels,
        text_of(definition) or None if definition is not None else None,
    )


def parse_pcs_tables(stream: IO[bytes]) -> Iterator[PcsTable]:
    for event, element in stream_elements(stream):
        if event != "end" or element.tag != "pcsTable":
            continue
        head = [_axis(a) for a in element.findall("axis")]
        head.sort(key=lambda a: a[0])
        axes = [(title, labels[0][0], labels[0][1]) for _, title, labels, _ in head]
        rows = []
        for row in element.findall("pcsRow"):
            body = sorted((_axis(a) for a in row.findall("axis")), key=lambda a: a[0])
            rows.append(
                PcsRow(
                    axes=[(title, labels) for _, title, labels, _ in body],
                    codes=int(row.get("codes") or 0),
                )
            )
        yield PcsTable(
            code="".join(char for _, char, _ in axes),
            axes=axes,
            operation_definition=head[2][3] if len(head) > 2 else None,
            rows=rows,
        )
        element.clear()


def parse_pcs_definitions(stream: IO[bytes]) -> Iterator[PcsDefinition]:
    for event, element in stream_elements(stream):
        if event != "end" or element.tag != "section":
            continue
        section = (element.get("code") or "").strip()
        for axis in element.findall("axis"):
            pos = int(axis.get("pos") or 0)
            axis_title = text_of(axis.find("title"))
            for terms in axis.findall("terms"):
                definition = text_of(terms.find("definition")) or None
                explanation = text_of(terms.find("explanation")) or None
                includes = [text_of(i) for i in terms.findall("includes")]
                for title in terms.findall("title"):
                    yield PcsDefinition(
                        section=section,
                        pos=pos,
                        axis=axis_title,
                        label=text_of(title),
                        definition=definition,
                        explanation=explanation,
                        includes=includes,
                    )
        element.clear()


async def load_icd10pcs(
    sessions: async_sessionmaker[AsyncSession],
    source_dir: Path,
    release: Release = FY2027,
) -> LoadReport:
    """Replace the release's ICD-10-PCS rows in one transaction."""
    archives = Archives(source_dir)
    archives.check(TABLES_ZIP, ORDER_ZIP)
    report = LoadReport(system=ICD10PCS, version=release.version)
    common = {"system": ICD10PCS, "version": release.version}
    source = {"source": SOURCE, "source_version": release.version}
    table_codes: set[str] = set()
    counts = {"tables": 0, "table rows": 0}

    def table_rows(tables: Iterator[PcsTable], rows: list[dict[str, Any]]) -> Iterator[Any]:
        for table in tables:
            counts["tables"] += 1
            for number, row in enumerate(table.rows, 1):
                counts["table rows"] += 1
                table_codes.update(row.expand(table.code))
                rows.append(
                    {
                        **common,
                        "table_code": table.code,
                        "row_number": number,
                        "axes": [[title, [list(v) for v in labels]] for title, labels in row.axes],
                        "codes": row.codes,
                        **source,
                    }
                )
            yield {
                **common,
                "code": table.code,
                "axes": [list(a) for a in table.axes],
                "operation_definition": table.operation_definition,
                **source,
            }

    async with sessions.begin() as session:
        await _clear(session, release)

        tables_file = archives.source_file(TABLES_ZIP, TABLES_XML)
        pending_rows: list[dict[str, Any]] = []
        with archives.open(TABLES_ZIP, TABLES_XML) as stream:
            tables_file.rows = await insert_batches(
                session,
                PcsTableRow.__table__,  # type: ignore[arg-type]
                table_rows(parse_pcs_tables(stream), pending_rows),
            )
        await insert_batches(session, PcsRowRow.__table__, pending_rows)  # type: ignore[arg-type]
        report.counts["tables"] = counts["tables"]
        report.counts["table rows"] = counts["table rows"]
        report.counts["codes the tables make"] = len(table_codes)

        definitions_file = archives.source_file(TABLES_ZIP, DEFINITIONS_XML)
        with archives.open(TABLES_ZIP, DEFINITIONS_XML) as stream:
            definitions = (
                {
                    **common,
                    "section": d.section,
                    "pos": d.pos,
                    "axis": d.axis,
                    "label": d.label,
                    "definition": d.definition,
                    "explanation": d.explanation,
                    "includes": d.includes,
                    **source,
                }
                for d in parse_pcs_definitions(stream)
            )
            definitions_file.rows = await insert_batches(
                session,
                PcsDefinitionRow.__table__,  # type: ignore[arg-type]
                definitions,
            )
        report.counts["definitions"] = definitions_file.rows

        order_file = archives.source_file(ORDER_ZIP, ORDER_TXT)
        valid: set[str] = set()
        sample = Sampler()

        def titles(stream: IO[bytes]) -> Iterator[dict[str, Any]]:
            for line in parse_order(stream):
                if line.valid:
                    valid.add(line.code)
                    sample.offer(f"{line.code} in tables={line.code in table_codes} {line.title!r}")
                yield {
                    **common,
                    "code": line.code,
                    "title": line.title,
                    "billable": line.valid,
                    "notes": {},
                    **source,
                }

        with archives.open(ORDER_ZIP, ORDER_TXT) as stream:
            order_file.rows = await insert_batches(
                session,
                IcdTabularRow.__table__,  # type: ignore[arg-type]
                titles(stream),
            )
        report.counts["order file codes"] = order_file.rows
        report.counts["valid codes"] = len(valid)
        report.files = [tables_file, definitions_file, order_file]
        record_loads(session, ICD10PCS, release, report.files)
        await session.flush()
        await analyze(
            session, "pcs_tables", "pcs_rows", "pcs_definitions", "icd_tabular", "kb_source_loads"
        )

    report.checks = {
        "table codes missing from the order file": str(len(table_codes - valid)),
        "valid order codes no table makes": str(len(valid - table_codes)),
    }
    report.sample = sample.rows
    return report


async def _clear(session: AsyncSession, release: Release) -> None:
    for model in (PcsTableRow, PcsRowRow, PcsDefinitionRow, IcdTabularRow, KbSourceLoadRow):
        await session.execute(
            delete(model).where(model.system == ICD10PCS, model.version == release.version)
        )
