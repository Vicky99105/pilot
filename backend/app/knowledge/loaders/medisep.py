"""Kerala MEDISEP Phase II package list (in force from 01.02.2026), Annexure IA, pp.8-370 of
the Legislative Assembly answer of 22.06.2026. Pages 3-7 and 382-388 are images and are not
read.

Each row becomes a los_norms row (the length of stay; "Day care" is 0 days) and a
package_masters row (scheme MEDISEP) with three package_items, one per accreditation level:
no NABH, entry-level NABH, and fully accredited NABH or a government medical college. The
procedure label marks add-on and stand-alone procedures.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import LosNormRow, PackageItemRow, PackageMasterRow
from app.knowledge.loaders.common import LoadReport, Sampler, analyze
from app.knowledge.loaders.hbp import join_cell, los_days
from app.knowledge.loaders.pdf import (
    ReferenceRelease,
    Table,
    amount,
    clear_release,
    norm_key,
    page_tables,
    percent,
    record_release,
    search_text,
    source_file,
    source_path,
)

SOURCE = "s6_medisep/medisep_phase2_niyamasabha.pdf"
PAGES = range(8, 371)
SCHEME = "MEDISEP"
RELEASE = ReferenceRelease("medisep", "PhaseII-2026", date(2026, 2, 1))
LEVELS = ("no_nabh", "entry_nabh", "nabh_full")

_CODE = re.compile(r"^MS\d{4}$")


@dataclass
class MedisepRow:
    code: str
    specialty: str
    package_name: str
    name: str
    los_text: str
    los_days: int | None
    implant: str | None
    implant_cost: str | None
    rates: dict[str, Decimal | None] = field(default_factory=dict)
    addon_applicable: bool | None = None
    label: str = ""
    addon: bool = False
    standalone: bool = False
    medical_or_surgical: str = ""
    page: int = 0


def _na(cell: str | None) -> str | None:
    text = join_cell(cell)
    return None if text.upper() in ("", "NA") else text


def parse_medisep_rows(tables: Iterable[tuple[int, Table]]) -> list[MedisepRow]:
    rows = []
    for page, table in tables:
        for cells in table:
            if len(cells) < 13 or not _CODE.match(join_cell(cells[0])):
                continue
            los = join_cell(cells[4])
            label = join_cell(cells[11])
            key = norm_key(label)
            rows.append(
                MedisepRow(
                    code=join_cell(cells[0]),
                    specialty=join_cell(cells[1]),
                    package_name=join_cell(cells[2]),
                    name=join_cell(cells[3]),
                    los_text=los,
                    los_days=0 if norm_key(los) == "daycare" else los_days(los),
                    implant=_na(cells[5]),
                    implant_cost=_na(cells[6]),
                    rates=dict(zip(LEVELS, (amount(c) for c in cells[7:10]), strict=True)),
                    addon_applicable={"yes": True, "no": False}.get(norm_key(join_cell(cells[10]))),
                    label=label,
                    addon=key.startswith("addon"),
                    standalone=key.startswith("standalone"),
                    medical_or_surgical=join_cell(cells[12]),
                    page=page,
                )
            )
    return rows


def _rising(rates: dict[str, Decimal | None]) -> bool:
    values = [rates[level] for level in LEVELS]
    known = [v for v in values if v is not None]
    return len(known) == len(values) and known == sorted(known)


async def load_medisep(sessions: async_sessionmaker[AsyncSession], source_dir: Path) -> LoadReport:
    path = source_path(source_dir, SOURCE)
    file = source_file(source_dir, SOURCE, "pp.8-370 Annexure IA")
    rows = parse_medisep_rows(page_tables(path, PAGES))
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    numbers = sorted(int(r.code[2:]) for r in rows)
    expected = set(range(numbers[0], numbers[-1] + 1)) if numbers else set()
    report.checks["codes"] = (
        f"MS{numbers[0]:04d}..MS{numbers[-1]:04d}, missing {len(expected - set(numbers))}, "
        f"repeated {len(numbers) - len(set(numbers))}"
        if numbers
        else "none found"
    )
    report.checks["rows with all three rates"] = percent(
        sum(1 for r in rows if all(v is not None for v in r.rates.values())), len(rows)
    )
    report.checks["rows with a LOS"] = percent(
        sum(1 for r in rows if r.los_days is not None), len(rows)
    )
    ordered = sum(1 for r in rows if _rising(r.rates))
    report.checks["rates rise with accreditation"] = percent(ordered, len(rows))

    sample = Sampler()
    source = {"source": RELEASE.system, "source_version": RELEASE.version}
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, PackageMasterRow, LosNormRow)
        result = await session.execute(
            insert(PackageMasterRow).returning(PackageMasterRow.id, sort_by_parameter_order=True),
            [
                {
                    "scheme": SCHEME,
                    "version": RELEASE.version,
                    "package_code": r.code,
                    "procedure_code": r.code,
                    "name": r.name,
                    "package_name": r.package_name,
                    "search_text": search_text(r.package_name, r.name),
                    "specialty": r.specialty,
                    "rate": r.rates["nabh_full"],
                    "rate_basis": "NABH fully accredited or government medical college",
                    "alos_days": r.los_days,
                    "los_text": r.los_text,
                    "implant": r.implant,
                    "implant_cost": r.implant_cost,
                    "addon": r.addon,
                    "standalone": r.standalone,
                    "medical_or_surgical": r.medical_or_surgical,
                    "extra": {"label": r.label, "addon_applicable": r.addon_applicable},
                    "page": r.page,
                    **source,
                }
                for r in rows
            ],
        )
        ids = list(result.scalars())
        items = [
            {
                "package_id": package_id,
                "tier": "all",
                "accreditation": level,
                "ward": "as_listed",
                "rate": r.rates[level],
                "page": r.page,
            }
            for package_id, r in zip(ids, rows, strict=True)
            for level in LEVELS
        ]
        await session.execute(insert(PackageItemRow), items)
        await session.execute(
            insert(LosNormRow),
            [
                {
                    "scheme": SCHEME,
                    "version": RELEASE.version,
                    "package_code": r.code,
                    "name": r.name,
                    "specialty": r.specialty,
                    "los_days": r.los_days,
                    "los_text": r.los_text,
                    "page": r.page,
                    **source,
                }
                for r in rows
            ],
        )
        for r in rows:
            sample.offer(
                f"{r.code} p{r.page} {r.name[:55]!r} [{r.specialty}] LOS {r.los_text!r} "
                f"rates {r.rates['no_nabh']}/{r.rates['entry_nabh']}/{r.rates['nabh_full']} "
                f"{r.label}"
            )
        file.rows = len(rows)
        record_release(session, RELEASE, [file])
        await session.flush()
        await analyze(session, "package_masters", "package_items", "los_norms")

    report.files = [file]
    report.counts = {
        "packages": len(rows),
        "LOS norms": len(rows),
        "package items": len(items),
        "add-on procedures": sum(1 for r in rows if r.addon),
        "stand-alone procedures": sum(1 for r in rows if r.standalone),
        "day care": sum(1 for r in rows if r.los_days == 0),
    }
    report.sample = sample.rows
    return report
