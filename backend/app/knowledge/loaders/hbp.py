"""PM-JAY Health Benefit Package 2022, as notified by the Nagaland State Health Agency.

Two Nagaland copies of the same list were downloaded. The 357-page copy prints every cell in
full; the 67-page copy clips long cells (names and implant notes stop at the cell edge), so the
357-page copy is loaded and the 67-page copy cross-checks it: codes, rates, LOS and flags must
agree, and each clipped name must be the start of the full one. NAG* codes are Nagaland's own
state packages and are dropped.

Rows come from pdfplumber's table cells, so a wrapped name is one cell with line breaks. A
cell's lines are joined with spaces, except that a hyphen at a line end stays joined to the
next line and a one- or two-letter tail cut from a word ("Reconstructiv" / "e") is joined back
to it. The loader measures the result: the share of names whose letters appear, in order, in
the page's own text.

Rates are the Tier 3 (Z) city rate for a general ward. HBP 2022 marks no add-on or stand-alone
flags in this list; those columns stay empty and are reported.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import PackageMasterRow
from app.knowledge.loaders.common import LoadReport, Sampler, analyze
from app.knowledge.loaders.pdf import (
    NOT_STATED,
    ReferenceRelease,
    Table,
    amount,
    clear_release,
    norm_key,
    page_count,
    page_tables,
    page_texts,
    percent,
    record_release,
    search_text,
    source_file,
    source_path,
)

PRIMARY = "s5_hbp_nagaland_341/nagaland_1743872546_341.pdf"
CROSS_CHECK = "s5_hbp_nagaland_381/nagaland_381.pdf"
SCHEME = "PMJAY"
RELEASE = ReferenceRelease("hbp", "HBP2022", NOT_STATED)
RATE_BASIS = "Tier 3 (Z) city, general ward"
STATE_ONLY = "NAG"

_CODE = re.compile(r"^([A-Z]{2,3}\d{2,4})[A-Z]{0,2}$")
_WORDS_NOT_TAILS = {"a", "an", "as", "at", "by", "in", "is", "of", "on", "or", "to", "up"}


@dataclass
class HbpRow:
    specialty: str
    specialty_code: str
    package_name: str
    procedure_code: str
    package_code: str
    name: str
    rate: Decimal | None
    implant: str | None
    implant_cost: str | None
    stratification: bool | None
    stratification_remarks: str | None
    multiple_procedures: bool | None
    public_only: bool | None
    los_text: str
    alos_days: int | None
    page: int


def join_cell(cell: str | None) -> str:
    if not cell:
        return ""
    lines = [line.strip() for line in cell.replace("￾", "-\n").split("\n")]
    out = ""
    for line in (ln for ln in lines if ln):
        if not out:
            out = line
        elif out.endswith("-") and line[:1].islower():
            out += line
        elif (
            out[-1:].isalpha()
            and re.fullmatch(r"[a-z]{1,2}", line.split(" ")[0])
            and line.split(" ")[0] not in _WORDS_NOT_TAILS
        ):
            out += line
        else:
            out += " " + line
    return " ".join(out.split())


def _flag(cell: str | None) -> bool | None:
    key = norm_key(cell or "")
    if key in ("yes", "y"):
        return True
    if key in ("no", "n"):
        return False
    return None


def _na(cell: str | None) -> str | None:
    text = join_cell(cell)
    return None if text.upper() in ("", "NA", "N/A") else text


def los_days(text: str) -> int | None:
    """Days from the LOS cell: a number, or the upper end of a range ("3-5 days" -> 5)."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    return max(numbers) if numbers else None


def parse_hbp_rows(tables: Iterable[tuple[int, Table]]) -> tuple[list[HbpRow], list[str]]:
    """(package rows, dropped state-only codes). Rows without a procedure code (headers,
    notes) are skipped."""
    rows: list[HbpRow] = []
    dropped: list[str] = []
    for page, table in tables:
        for cells in table:
            if len(cells) < 13:
                continue
            code = re.sub(r"^[^A-Z]+", "", join_cell(cells[3]).replace(" ", ""))
            match = _CODE.match(code)
            if not match:
                continue
            if code.startswith(STATE_ONLY):
                dropped.append(code)
                continue
            los = join_cell(cells[12])
            rows.append(
                HbpRow(
                    specialty=join_cell(cells[0]),
                    specialty_code=join_cell(cells[1]),
                    package_name=join_cell(cells[2]),
                    procedure_code=code,
                    package_code=match[1],
                    name=join_cell(cells[4]),
                    rate=amount(cells[5]),
                    implant=_na(cells[6]),
                    implant_cost=_na(cells[7]),
                    stratification=_flag(cells[8]),
                    stratification_remarks=_na(cells[9]),
                    multiple_procedures=_flag(cells[10]),
                    public_only=_flag(cells[11]),
                    los_text=los,
                    alos_days=los_days(los),
                    page=page,
                )
            )
    return rows, dropped


def _key(row: HbpRow) -> tuple[str, str]:
    return row.specialty_code, row.procedure_code


async def load_hbp(sessions: async_sessionmaker[AsyncSession], source_dir: Path) -> LoadReport:
    path = source_path(source_dir, PRIMARY)
    pages = page_count(path)
    primary = source_file(source_dir, PRIMARY, f"pp.1-{pages}")
    rows, dropped = parse_hbp_rows(page_tables(path, range(1, pages + 1)))
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    files = [primary]

    # Name repair accuracy: the name's letters, in order, appear in its page's text.
    texts = {p: norm_key(t) for p, t in page_texts(path, range(1, pages + 1))}
    found = sum(
        1
        for r in rows
        if norm_key(r.name) in texts.get(r.page, "")
        or norm_key(r.name) in texts.get(r.page, "") + texts.get(r.page + 1, "")
    )
    report.checks["names found verbatim (spacing aside) in the page text"] = percent(
        found, len(rows)
    )

    if (source_dir / CROSS_CHECK).is_file():
        cross_path = source_dir / CROSS_CHECK
        cross_pages = page_count(cross_path)
        files.append(
            source_file(source_dir, CROSS_CHECK, f"pp.1-{cross_pages} cross-check, not loaded")
        )
        other, _ = parse_hbp_rows(page_tables(cross_path, range(1, cross_pages + 1)))
        mine = {_key(r): r for r in rows}
        theirs = {_key(r): r for r in other}
        both = set(mine) & set(theirs)
        report.checks["cross-check codes in both copies"] = (
            f"{len(both)}; only primary {len(set(mine) - both)}; only copy "
            f"{len(set(theirs) - both)}"
        )
        for label, field in (
            ("rates", "rate"),
            ("LOS", "los_text"),
            ("public-only flags", "public_only"),
            ("multiple-procedure flags", "multiple_procedures"),
        ):
            agree = sum(
                1
                for k in both
                if (
                    norm_key(str(getattr(mine[k], field)))
                    == norm_key(str(getattr(theirs[k], field)))
                )
            )
            report.checks[f"cross-check {label} agree"] = percent(agree, len(both))
        prefix = sum(1 for k in both if norm_key(mine[k].name).startswith(norm_key(theirs[k].name)))
        report.checks["cross-check copy's name is the start of the primary's"] = percent(
            prefix, len(both)
        )

    sample = Sampler()
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, PackageMasterRow)
        values = []
        for r in rows:
            values.append(
                {
                    "scheme": SCHEME,
                    "version": RELEASE.version,
                    "package_code": r.package_code,
                    "procedure_code": r.procedure_code,
                    "name": r.name,
                    "package_name": r.package_name,
                    "search_text": search_text(r.package_name, r.name),
                    "specialty": r.specialty,
                    "specialty_code": r.specialty_code,
                    "rate": r.rate,
                    "rate_basis": RATE_BASIS,
                    "alos_days": r.alos_days,
                    "los_text": r.los_text or None,
                    "implant": r.implant,
                    "implant_cost": r.implant_cost,
                    "stratification": r.stratification,
                    "multiple_procedures": r.multiple_procedures,
                    "addon": None,
                    "standalone": None,
                    "public_only": r.public_only,
                    "extra": {"stratification_remarks": r.stratification_remarks}
                    if r.stratification_remarks
                    else {},
                    "page": r.page,
                    "source": RELEASE.system,
                    "source_version": RELEASE.version,
                }
            )
            sample.offer(
                f"{r.specialty_code}/{r.procedure_code} p{r.page} {r.name[:60]!r} Rs {r.rate} "
                f"LOS {r.los_text!r} implant={r.implant is not None} "
                f"multi={r.multiple_procedures} public_only={r.public_only}"
            )
        for start in range(0, len(values), 2000):
            await session.execute(insert(PackageMasterRow), values[start : start + 2000])
        primary.rows = len(values)
        record_release(session, RELEASE, files)
        await session.flush()
        await analyze(session, "package_masters", "kb_source_loads")

    report.files = files
    report.counts = {
        "package rows": len(rows),
        "unique procedure codes": len({r.procedure_code for r in rows}),
        "unique package codes": len({r.package_code for r in rows}),
        "state-only rows dropped": len(dropped),
        "rows with an implant": sum(1 for r in rows if r.implant),
        "rows with a zero rate (stratified per-day rates)": sum(1 for r in rows if r.rate == 0),
    }
    report.checks["add-on and stand-alone flags"] = "not in this source; left empty"
    report.sample = sample.rows
    return report
