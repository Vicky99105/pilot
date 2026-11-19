"""CGHS 2025 rate lists (OM 03.10.2025, Annexure I, in force from 13.10.2025), the city
tier mapping (OM 09.10.2025) and the clarification of 24.03.2026.

Annexure I prints one rate list per city tier: X (Tier I) pp.7-108, Y (Tier II) pp.112-211,
Z (Tier III) pp.215-314. Each row is serial number, code, name, the Non-NABH, NABH and
super-speciality rates for a semi-private ward, and the speciality. Names and specialities
wrap over lines, so a row starts at a line beginning with the serial number and code and runs
to the next such line; the three rates are the last three numbers followed by the speciality.

Each code becomes one package_masters row (the NABH rate in an X city as the base rate) with
nine package_items: three tiers by three accreditation levels. The differentials the OM states
(Non-NABH 15% lower, super-speciality 15% higher, Y and Z 10% and 20% lower than X, ward
entitlement, multi-procedure) become rate_factors rows, each found in the OM text; the loader
checks that the printed rates follow them. City tiers become rate_factors rows too.

Changes applied from the later OMs:
- 09.10.2025: Faridabad, Ghaziabad, Noida and Gurugram move from Y to X; Jalandhar Cantt
  goes with Jalandhar (Y); Goa and Port Blair are Y; Panchkula and S.A.S. Nagar (Mohali) go
  with Chandigarh (Y). No rate changes.
- 24.03.2026: RI134 is renamed; the OPD super-specialist note in Annexure 2 is reworded.
  No rate changes.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import PackageItemRow, PackageMasterRow, RateFactorRow
from app.knowledge.loaders.common import LoadReport, Sampler, analyze
from app.knowledge.loaders.pdf import (
    ReferenceRelease,
    clean,
    clear_release,
    lines_of,
    norm_key,
    page_texts,
    percent,
    record_release,
    search_text,
    source_file,
    source_path,
)

RATES = "s4_cghs_om_2025_10_03/cghs_om_2025-10-03_rates.pdf"
TIER_OM = "s4_cghs_om_2025_10_09_tier/cghs_om_2025-10-09_tier_mapping.pdf"
CLARIFICATION = "s4_cghs_clarif_2026_03_24/cghs_om_2026-03-24_clarification.pdf"
TIER_PAGES = {"X": range(7, 109), "Y": range(112, 212), "Z": range(215, 315)}
SCHEME = "CGHS"
RELEASE = ReferenceRelease("cghs", "2025-10-13", date(2025, 10, 13))
WARD = "semi_private"
ACCREDITATION = ("non_nabh", "nabh", "super_speciality")

_ROW = re.compile(r"^(\d{1,4})\s+([A-Z]{2,3}\d{3}[A-Z]?)\b\s*(.*)$")
_RATE = r"(\d[\d,]*(?:\.\d+)?|NA|-)"
_RATES = re.compile(rf"(?:(?<=\s)|^){_RATE}\s+{_RATE}\s+{_RATE}(?=\s+[A-Za-z]|\s*$)")
_NOISE = re.compile(r"^5-16/CGHS|^CGHS rates for Tier|^\d{1,3}$", re.IGNORECASE)  # page no.
_HEADER_START = re.compile(r"^Sr\.?$")

# Y (Tier II) cities as Annexure I prints them (from the Department of Expenditure OM of
# 07.07.2017). The page wraps names mid-word, so the loader checks each one against the page
# with spaces removed rather than parsing the grid.
Y_CITIES = [
    "Vijayawada", "Warangal", "Visakhapatnam", "Guntur", "Nellore", "Guwahati", "Patna",
    "Chandigarh", "Durg-Bhilai", "Raipur", "Rajkot", "Jamnagar", "Bhavnagar", "Vadodara",
    "Surat", "Faridabad", "Gurgaon", "Srinagar", "Jammu", "Jamshedpur", "Dhanbad", "Ranchi",
    "Bokaro", "Belgaum", "Hubli-Dharwad", "Mangalore", "Mysore", "Gulbarga", "Kozhikode",
    "Kochi", "Thiruvananthapuram", "Thrissur", "Malappuram", "Kannur", "Kollam", "Gwalior",
    "Indore", "Bhopal", "Jabalpur", "Ujjain", "Amravati", "Nagpur", "Aurangabad", "Nashik",
    "Bhiwandi", "Solapur", "Kolhapur", "Vasai-Virar", "Malegaon", "Nanded-Waghala", "Sangli",
    "Cuttack", "Bhubaneswar", "Raurkela", "Puducherry", "Amritsar", "Jalandhar", "Ludhiana",
    "Bikaner", "Jaipur", "Jodhpur", "Kota", "Ajmer", "Salem", "Tiruppur", "Coimbatore",
    "Tiruchirappalli", "Madurai", "Erode", "Moradabad", "Meerut", "Ghaziabad", "Aligarh",
    "Agra", "Bareilly", "Lucknow", "Kanpur", "Allahabad", "Gorakhpur", "Varanasi",
    "Saharanpur", "Noida", "Firozabad", "Jhansi", "Dehradun", "Asansol", "Siliguri",
    "Durgapur",
]  # fmt: skip

# OM 09.10.2025: city -> tier.
TIER_OM_CHANGES = {
    "faridabad": "X",
    "ghaziabad": "X",
    "noida": "X",
    "gurgaon": "X",  # printed "Gurugram" in the OM, "Gurgaon" in Annexure I
    "jalandhar cantt": "Y",
    "goa": "Y",
    "port blair": "Y",
    "panchkula": "Y",
    "s.a.s. nagar": "Y",
}
TIER_OM_WORDS = ["Faridabad", "Ghaziabad", "Noida", "Gurugram", "Jalandhar Cantt", "Goa",
                 "Port Blair", "Panchkula", "S.A.S. Nagar"]  # fmt: skip

# (factor, key, value, phrase that must be in the OM, page, note)
FACTORS: list[tuple[str, str, Decimal, str, int, str]] = [
    ("accreditation", "non_nabh", Decimal("0.85"), "15% lower than NABH", 1,
     "Non-NABH rate = NABH rate less 15%"),
    ("accreditation", "nabh", Decimal("1.00"), "15% lower than NABH", 1, "the reference rate"),
    ("accreditation", "super_speciality", Decimal("1.15"), "15% higher than those", 1,
     "super-speciality rate = NABH rate plus 15%, for super specialities"),
    ("tier", "X", Decimal("1.00"), "10% and 20% respectively lower", 1, "the reference tier"),
    ("tier", "Y", Decimal("0.90"), "10% and 20% respectively lower", 1,
     "Y rate = X rate less 10%; also North-East, Jammu and Kashmir, Ladakh"),
    ("tier", "Z", Decimal("0.80"), "10% and 20% respectively lower", 1,
     "Z rate = X rate less 20%"),
    ("ward", "general", Decimal("0.95"), "decrease of 5% in the rates", 2,
     "not for consultations, radiotherapy, investigations, day care and minor procedures"),
    ("ward", "semi_private", Decimal("1.00"), "decrease of 5% in the rates", 2,
     "the rates are for a semi-private ward"),
    ("ward", "private", Decimal("1.05"), "increase of 5%", 2,
     "not for consultations, radiotherapy, investigations, day care and minor procedures"),
    ("multi_procedure", "first", Decimal("1.00"), "100% of its package", 5,
     "same OT session: the highest-rated surgery at 100%"),
    ("multi_procedure", "second", Decimal("0.50"), "50% of its package", 5,
     "same OT session: the second surgery at 50%"),
    ("multi_procedure", "third_and_more", Decimal("0.25"), "25% of each respective", 5,
     "same OT session: third and later surgeries at 25% each"),
    ("multi_procedure", "bilateral_second", Decimal("0.50"), "Second procedure at 50%", 5,
     "identical surgery at a second site in one session at 50%"),
    ("multi_procedure", "within_package_period", Decimal("0.75"), "75% of its applicable", 6,
     "a later procedure within an earlier one's package period at 75%"),
    ("package_days", "super_speciality", Decimal("12"), "12 days for Specialized", 6,
     "days of indoor stay a package covers"),
    ("package_days", "major_surgery", Decimal("7"), "7 days for other Major", 6,
     "days of indoor stay a package covers"),
    ("package_days", "laparoscopic_angioplasty_normal_delivery", Decimal("3"), "3 days for", 6,
     "days of indoor stay a package covers"),
    ("package_days", "day_care_minor", Decimal("1"), "1 day for day care", 6,
     "days of indoor stay a package covers"),
]  # fmt: skip

# The 24.03.2026 clarification's renaming, applied to every tier.
RENAMED = {
    "RI134": "MRI Dorsal/Dorso Lumbar Spine/Cervical/Cervico Dorsal Spine - Without Contrast"
}


@dataclass
class RateRow:
    tier: str
    serial: int
    code: str
    name: str
    non_nabh: Decimal | None
    nabh: Decimal | None
    super_speciality: Decimal | None
    specialty: str
    page: int


def _rate(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def parse_rate_rows(pages: Iterable[tuple[int, str]], tier: str) -> list[RateRow]:
    blocks: list[list[Any]] = []  # [serial, code, text, page]
    for page, text in pages:
        in_header = False
        for line in lines_of(text):
            if _NOISE.search(line):
                continue
            if _HEADER_START.match(line):
                in_header = True
                continue
            row = _ROW.match(line)
            if row:
                in_header = False
                blocks.append([int(row[1]), row[2], row[3], page])
                continue
            if in_header or not blocks:
                continue
            blocks[-1][2] = f"{blocks[-1][2]} {line}"
    rows = []
    for serial, code, text, page in blocks:
        text = clean(text)
        matches = list(_RATES.finditer(text))
        if not matches:
            raise ValueError(f"CGHS {tier} row {serial} {code}: no rates in {text!r}")
        last = matches[-1]
        rows.append(
            RateRow(
                tier=tier,
                serial=serial,
                code=code,
                name=text[: last.start()].strip(),
                non_nabh=_rate(last[1]),
                nabh=_rate(last[2]),
                super_speciality=_rate(last[3]),
                specialty=text[last.end() :].strip(),
                page=page,
            )
        )
    return rows


def _name(tiers_of: dict[str, RateRow]) -> str:
    """The X list's name, or another tier's when the X row's name was cut off at a page top
    (the name printed above the serial line, under the repeated header)."""
    return next(
        (tiers_of[t].name for t in ("X", "Y", "Z") if t in tiers_of and tiers_of[t].name), ""
    )


def tier_consistency(base: list[RateRow], other: list[RateRow], factor: Decimal) -> tuple[int, int]:
    """How many of other's NABH rates are base's times factor, within Rs 1 of rounding."""
    by_code = {r.code: r for r in base}
    compared = consistent = 0
    for row in other:
        reference = by_code.get(row.code)
        if reference is None or reference.nabh is None or row.nabh is None:
            continue
        compared += 1
        if abs(reference.nabh * factor - row.nabh) <= 1:
            consistent += 1
    return consistent, compared


def accreditation_consistency(rows: list[RateRow]) -> tuple[int, int]:
    """Rows whose Non-NABH rate is the NABH rate less 15% (within Rs 1)."""
    pairs = [(r.non_nabh, r.nabh) for r in rows if r.non_nabh is not None and r.nabh]
    good = sum(1 for low, high in pairs if abs(high * Decimal("0.85") - low) <= 1)
    return good, len(pairs)


def find_x_cities(text: str) -> list[str]:
    return re.findall(r"([A-Z][A-Za-z]+) \(UA\)", " ".join(lines_of(text)))


def verify_cities(cities: list[str], text: str) -> tuple[list[str], list[str]]:
    """(found, missing): each city looked up in the page with all spacing removed."""
    page = norm_key(text)
    found = [c for c in cities if norm_key(c) in page]
    return found, [c for c in cities if c not in found]


def apply_tier_om(tiers: dict[str, str]) -> list[tuple[str, str | None, str]]:
    """Apply the 09.10.2025 reclassification in place; return (city, old tier, new tier) for
    each change."""
    changed = []
    for city, tier in TIER_OM_CHANGES.items():
        old = tiers.get(city)
        if old != tier:
            changed.append((city, old, tier))
        tiers[city] = tier
    return changed


async def load_cghs(sessions: async_sessionmaker[AsyncSession], source_dir: Path) -> LoadReport:
    rates_path = source_path(source_dir, RATES)
    rates_file = source_file(source_dir, RATES, "pp.1-6 rules, 7-108 X, 112-211 Y, 215-314 Z")
    tier_file = source_file(source_dir, TIER_OM, "whole OM")
    clarification_file = source_file(source_dir, CLARIFICATION, "whole OM")
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)

    rule_pages = dict(page_texts(rates_path, range(1, 7)))
    rows = {
        tier: parse_rate_rows(page_texts(rates_path, pages), tier)
        for tier, pages in TIER_PAGES.items()
    }
    city_pages = dict(page_texts(rates_path, [4, 109, 110]))
    tier_text = " ".join(t for _, t in page_texts(source_path(source_dir, TIER_OM), range(1, 3)))
    clarification = " ".join(
        t for _, t in page_texts(source_path(source_dir, CLARIFICATION), range(1, 3))
    )

    # Integrity: serial numbers run 1..N with no gap, and every tier lists the same codes.
    for tier, tier_rows in rows.items():
        serials = [r.serial for r in tier_rows]
        gaps = sum(1 for a, b in pairwise(serials) if b != a + 1)
        report.counts[f"tier {tier} rows"] = len(tier_rows)
        report.checks[f"tier {tier} serials"] = f"1..{max(serials)}, gaps {gaps}"
    x_codes = [r.code for r in rows["X"]]
    for tier in ("Y", "Z"):
        same = [r.code for r in rows[tier]] == x_codes
        names = sum(
            1
            for a, b in zip(rows["X"], rows[tier], strict=False)
            if norm_key(a.name) == norm_key(b.name)
        )
        report.checks[f"tier {tier} codes in X order"] = "same" if same else "DIFFER"
        report.checks[f"tier {tier} names agree with X"] = percent(names, len(x_codes))
    for tier, factor in (("Y", Decimal("0.90")), ("Z", Decimal("0.80"))):
        good, compared = tier_consistency(rows["X"], rows[tier], factor)
        report.checks[f"tier {tier} NABH = X x {factor}"] = percent(good, compared)
    good, compared = accreditation_consistency(rows["X"])
    report.checks["tier X Non-NABH = NABH x 0.85"] = percent(good, compared)
    report.checks["rates uniform across tiers (consultations, ICU, blood components)"] = str(
        sum(1 for a, b in zip(rows["X"], rows["Y"], strict=False) if a.nabh == b.nabh)
    )
    report.checks["X names taken from another tier (cut at a page top)"] = (
        ", ".join(r.code for r in rows["X"] if not r.name) or "none"
    )

    renamed = []
    for code, name in RENAMED.items():
        if norm_key(name)[:30] not in norm_key(clarification):
            report.checks[f"clarification {code}"] = "new name not found in the OM: not applied"
            continue
        for tier_rows in rows.values():
            for row in tier_rows:
                if row.code == code:
                    renamed.append((tier, row.name))
                    row.name = name
        report.checks[f"clarification {code} renamed"] = (
            f"{renamed[0][1]!r} -> {name!r}" if renamed else "code not found"
        )
    report.checks["clarification rate changes"] = "none (the OM changes descriptions only)"

    rules_text = " ".join(" ".join(lines_of(t)) for t in rule_pages.values())
    factors = [f for f in FACTORS if f[3].lower() in rules_text.lower()]
    report.checks["rate factors not found in the OM"] = (
        ", ".join(f"{f[0]}:{f[1]}" for f in FACTORS if f not in factors) or "none"
    )

    tiers = {c.lower(): "X" for c in find_x_cities(city_pages.get(4, ""))}
    found_y, missing_y = verify_cities(
        Y_CITIES, " ".join(city_pages.get(p, "") for p in (109, 110))
    )
    for city in found_y:
        tiers.setdefault(city.lower(), "Y")
    report.checks["Y cities found on the page"] = percent(len(found_y), len(Y_CITIES))
    if missing_y:
        report.checks["Y cities not found (not loaded)"] = ", ".join(missing_y)
    _, om_missing = verify_cities(TIER_OM_WORDS, tier_text)
    changed = apply_tier_om(tiers) if not om_missing else []
    report.checks["tier OM 09.10.2025 changes"] = (
        "; ".join(f"{c}: {old or 'unlisted'} -> {new}" for c, old, new in changed)
        if changed
        else f"not applied, cities not found in the OM: {om_missing}"
    )

    sample = Sampler()
    source = {"source": RELEASE.system, "source_version": RELEASE.version}
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, PackageMasterRow, RateFactorRow)
        by_code: dict[str, dict[str, RateRow]] = {}
        for tier, tier_rows in rows.items():
            for row in tier_rows:
                by_code.setdefault(row.code, {})[tier] = row
        masters = [
            {
                "scheme": SCHEME,
                "version": RELEASE.version,
                "package_code": code,
                "procedure_code": code,
                "name": _name(tiers_of),
                "search_text": search_text(_name(tiers_of)),
                "specialty": tiers_of["X"].specialty,
                "rate": tiers_of["X"].nabh,
                "rate_basis": "NABH, X (Tier I) city, semi-private ward",
                "extra": {"serial": tiers_of["X"].serial},
                "page": tiers_of["X"].page,
                **source,
            }
            for code, tiers_of in by_code.items()
            if "X" in tiers_of
        ]
        result = await session.execute(
            insert(PackageMasterRow).returning(
                PackageMasterRow.id, PackageMasterRow.procedure_code
            ),
            masters,
        )
        ids = {code: package_id for package_id, code in result.all()}
        items = []
        for code, tiers_of in by_code.items():
            for tier, row in tiers_of.items():
                for accreditation in ACCREDITATION:
                    items.append(
                        {
                            "package_id": ids[code],
                            "tier": tier,
                            "accreditation": accreditation,
                            "ward": WARD,
                            "rate": getattr(row, accreditation),
                            "page": row.page,
                        }
                    )
                if tier == "X":
                    sample.offer(
                        f"{code} p{row.page} {row.name[:60]!r} [{row.specialty}] X "
                        f"{row.non_nabh}/{row.nabh}/{row.super_speciality} Y "
                        f"{tiers_of.get('Y') and tiers_of['Y'].nabh} Z "
                        f"{tiers_of.get('Z') and tiers_of['Z'].nabh}"
                    )
        for start in range(0, len(items), 5000):
            await session.execute(insert(PackageItemRow), items[start : start + 5000])
        factor_rows = [
            RateFactorRow(scheme=SCHEME, version=RELEASE.version, factor=factor, key=key,
                          value=value, note=note, page=page, **source)
            for factor, key, value, _, page, note in factors
        ]  # fmt: skip
        changed_cities = {c for c, _, _ in changed}
        for city, tier in sorted(tiers.items()):
            factor_rows.append(
                RateFactorRow(
                    scheme=SCHEME,
                    version=RELEASE.version,
                    factor="city_tier",
                    key=city,
                    text_value=tier,
                    note="OM 09.10.2025" if city in changed_cities else "Annexure I",
                    page=None if city in changed_cities else (4 if tier == "X" else 109),
                    **source,
                )
            )
        factor_rows.append(
            RateFactorRow(
                scheme=SCHEME,
                version=RELEASE.version,
                factor="city_tier",
                key="*",
                text_value="Z",
                note="every city not listed as X or Y",
                page=215,
                **source,
            )
        )
        session.add_all(factor_rows)
        rates_file.rows = len(masters)
        tier_file.rows = len(changed)
        clarification_file.rows = len(renamed)
        record_release(session, RELEASE, [rates_file, tier_file, clarification_file])
        await session.flush()
        await analyze(session, "package_masters", "package_items", "rate_factors")

    report.files = [rates_file, tier_file, clarification_file]
    report.counts["package masters"] = len(masters)
    report.counts["package items"] = len(items)
    report.counts["rate factors"] = len(factors)
    report.counts["city tiers"] = len(tiers)
    report.sample = sample.rows
    return report
