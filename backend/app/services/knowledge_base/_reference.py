"""Lookups over the public reference tables (slice P2-3). Private to the knowledge base
service.

Name lookups narrow the rows in the database with ILIKE on the query's longer words (served by
the trigram indexes on Postgres), then rank the few rows that come back with rapidfuzz in
Python, so ranking is the same on Postgres and on SQLite.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    DrugPriceRow,
    ExclusionRow,
    GuidelineSectionRow,
    HospitalAgreementRow,
    HospitalRow,
    LosNormRow,
    NlemMedicineRow,
    NonPayableItemRow,
    PackageCrosswalkRow,
    PackageItemRow,
    PackageMasterRow,
    RateFactorRow,
)
from app.services.knowledge_base.contract import (
    MAX_NON_PAYABLE_CANDIDATES,
    MAX_TARIFF_CANDIDATES,
    DrugCheck,
    DrugPrice,
    Exclusion,
    GuidelineSection,
    HospitalAgreement,
    LosNorm,
    NonPayableCandidate,
    PackageCrosswalk,
    TariffCandidate,
    TariffRate,
)
from app.shared.codes import code_in_ranges

CENTS = Decimal("0.01")
FIXTURE = "fixture"
CGHS = "CGHS"
ROWS_PER_NAME = 300  # rows fetched per name before ranking
NAME_SCORE = 60.0  # a package name match at or above this is a candidate
LOS_NAME_SCORE = 80.0
DRUG_NAME_SCORE = 85.0
NON_PAYABLE_SCORE = 80.0

# Common names for cities that the rate lists spell another way.
CITY_ALIASES = {
    "new delhi": "delhi",
    "gurugram": "gurgaon",
    "bangalore": "bengaluru",
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "mohali": "s.a.s. nagar",
    "prayagraj": "allahabad",
    "vizag": "visakhapatnam",
    "trivandrum": "thiruvananthapuram",
    "cochin": "kochi",
    "mysuru": "mysore",
    "mangaluru": "mangalore",
    "belagavi": "belgaum",
    "kalaburagi": "gulbarga",
}
_STOPWORDS = {"with", "without", "and", "for", "the", "under", "inj", "tab", "cap", "syp"}
_CODE = re.compile(r"^[A-Z]{2,3}\d{2,4}[A-Z]{0,2}$")
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|gm|g|mcg|µg|ml|iu|%)\b", re.IGNORECASE)
_FORMS = {
    "injection": ("inj", "injection", "vial", "ampoule", "amp"),
    "tablet": ("tab", "tablet", "tablets"),
    "capsule": ("cap", "capsule", "capsules"),
    "oral liquid": ("syp", "syrup", "suspension", "liquid"),
}


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _words(text: str) -> list[str]:
    return [w for w in _key(text).split() if len(w) >= 4 and w not in _STOPWORDS]


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(CENTS)


# --- Tariffs ------------------------------------------------------------------------------


def _name_score(query: str, text: str) -> float:
    """The mean of the token-set and token-sort ratios: the set ratio alone scores every
    "Laparoscopic X" as high as the procedure asked for; the sort ratio penalises the words
    that differ."""
    return (
        float(fuzz.token_set_ratio(query, text)) + float(fuzz.token_sort_ratio(query, text))
    ) / 2


async def hospital_tier(session: AsyncSession, hospital_id: str | None) -> str | None:
    if not hospital_id:
        return None
    city = await session.scalar(
        select(HospitalRow.city).where(HospitalRow.rohini_id == hospital_id)
    )
    if city is None:
        return None
    key = city.strip().lower()
    key = CITY_ALIASES.get(key, key)
    rows = dict(
        (
            await session.execute(
                select(RateFactorRow.key, RateFactorRow.text_value).where(
                    RateFactorRow.scheme == CGHS,
                    RateFactorRow.factor == "city_tier",
                    RateFactorRow.key.in_([key, "*"]),
                )
            )
        ).all()
    )
    return rows.get(key) or rows.get("*")


async def tariff_candidates(
    session: AsyncSession, codes: list[str], names: list[str], hospital_id: str | None
) -> list[TariffCandidate]:
    tier = await hospital_tier(session, hospital_id)
    found: dict[int, tuple[PackageMasterRow, str, float]] = {}
    keys = [c.strip().upper() for c in codes if c.strip()]
    if keys:
        rows = await session.scalars(
            select(PackageMasterRow)
            .where(
                or_(
                    PackageMasterRow.procedure_code.in_(keys),
                    PackageMasterRow.package_code.in_(keys),
                )
            )
            .order_by(PackageMasterRow.scheme, PackageMasterRow.procedure_code, PackageMasterRow.id)
            .limit(MAX_TARIFF_CANDIDATES * 4)
        )
        for row in rows:
            found.setdefault(row.id, (row, "code", 100.0))
    for name in names:
        words = _words(name)
        if not words:
            continue
        rows = await session.scalars(
            select(PackageMasterRow)
            .where(or_(*(PackageMasterRow.search_text.ilike(f"%{w}%") for w in words)))
            .limit(ROWS_PER_NAME)
        )
        query = _key(name)
        for row in rows:
            score = _name_score(query, _key(row.search_text))
            if score >= NAME_SCORE and (row.id not in found or found[row.id][2] < score):
                if row.id not in found or found[row.id][1] != "code":
                    found[row.id] = (row, "name", score)
    ranked = sorted(found.values(), key=lambda f: (f[1] != "code", -f[2], f[0].scheme, f[0].id))
    # One candidate per scheme and procedure code (HBP lists a code under each specialty).
    seen: set[tuple[str, str]] = set()
    chosen = []
    for row, how, score in ranked:
        if (row.scheme, row.procedure_code) in seen:
            continue
        seen.add((row.scheme, row.procedure_code))
        chosen.append((row, how, score))
        if len(chosen) >= MAX_TARIFF_CANDIDATES:
            break
    items: dict[int, list[PackageItemRow]] = {}
    if chosen:
        for item in await session.scalars(
            select(PackageItemRow)
            .where(PackageItemRow.package_id.in_([row.id for row, _, _ in chosen]))
            .order_by(PackageItemRow.id)
        ):
            items.setdefault(item.package_id, []).append(item)
    return [_candidate(row, how, score, tier, items.get(row.id, [])) for row, how, score in chosen]


def _candidate(
    row: PackageMasterRow, how: str, score: float, tier: str | None, items: list[PackageItemRow]
) -> TariffCandidate:
    rate, basis = _money(row.rate), row.rate_basis
    applicable = items
    if row.scheme == CGHS and tier is not None:
        applicable = [i for i in items if i.tier == tier]
        nabh = next((i for i in applicable if i.accreditation == "nabh"), None)
        if nabh is not None:
            rate, basis = _money(nabh.rate), f"NABH, {tier} city tier, semi-private ward"
    return TariffCandidate(
        scheme=row.scheme,
        version=row.version,
        procedure_code=row.procedure_code,
        package_code=row.package_code,
        name=row.name,
        package_name=row.package_name,
        specialty=row.specialty,
        matched_on=how,
        score=score,
        rate=rate,
        rate_basis=basis,
        tier=tier if row.scheme == CGHS else None,
        rates=[
            TariffRate(tier=i.tier, accreditation=i.accreditation, ward=i.ward, rate=_money(i.rate))
            for i in applicable
        ],
        alos_days=row.alos_days,
        implant=row.implant,
        multiple_procedures=row.multiple_procedures,
        addon=row.addon,
        standalone=row.standalone,
        source=row.source,
        page=row.page,
    )


# --- Guidelines and LOS -------------------------------------------------------------------


async def get_guideline(session: AsyncSession, package_code: str) -> list[GuidelineSection]:
    code = package_code.strip().upper()
    rows = await session.scalars(
        select(GuidelineSectionRow)
        .where(or_(GuidelineSectionRow.package_code == code, GuidelineSectionRow.old_code == code))
        .order_by(
            GuidelineSectionRow.section, GuidelineSectionRow.file_name, GuidelineSectionRow.id
        )
    )
    return [
        GuidelineSection(
            package_code=r.package_code,
            old_code=r.old_code,
            procedure_name=r.procedure_name,
            section=r.section,
            items=list(r.items or []),
            match_score=r.match_score,
            file_name=r.file_name,
            page=r.page,
            source=r.source,
        )
        for r in rows
    ]


def _los(row: LosNormRow, how: str) -> LosNorm:
    return LosNorm(
        scheme=row.scheme,
        version=row.version,
        package_code=row.package_code,
        name=row.name,
        los_days=row.los_days,
        los_text=row.los_text,
        matched_on=how,
        source=row.source,
        page=row.page,
    )


async def get_los_norm(session: AsyncSession, code_or_name: str) -> LosNorm | None:
    text = code_or_name.strip()
    if _CODE.match(text.upper()):
        row = await session.scalar(
            select(LosNormRow)
            .where(LosNormRow.package_code == text.upper(), LosNormRow.los_days.is_not(None))
            .order_by(LosNormRow.id)
            .limit(1)
        )
        if row is not None:
            return _los(row, "code")
        master = await session.scalar(
            select(PackageMasterRow)
            .where(
                PackageMasterRow.procedure_code == text.upper(),
                PackageMasterRow.alos_days.is_not(None),
            )
            .order_by(PackageMasterRow.id)
            .limit(1)
        )
        if master is not None:
            return LosNorm(
                scheme=master.scheme,
                version=master.version,
                package_code=master.procedure_code,
                name=master.name,
                los_days=master.alos_days,
                los_text=master.los_text,
                matched_on="code",
                source=master.source,
                page=master.page,
            )
        return None
    words = _words(text)
    if not words:
        return None
    rows = await session.scalars(
        select(LosNormRow)
        .where(
            LosNormRow.los_days.is_not(None),
            or_(*(LosNormRow.name.ilike(f"%{w}%") for w in words)),
        )
        .limit(ROWS_PER_NAME)
    )
    query = _key(text)
    scored = [(float(fuzz.token_set_ratio(query, _key(r.name))), -r.id, r) for r in rows]
    best = max(scored, default=None, key=lambda s: (s[0], s[1]))
    return _los(best[2], "name") if best and best[0] >= LOS_NAME_SCORE else None


# --- Drugs --------------------------------------------------------------------------------


def _strengths(text: str) -> set[tuple[Decimal, str]]:
    found = set()
    for number, unit in _STRENGTH.findall(text):
        unit = unit.lower()
        value = Decimal(number)
        if unit in ("g", "gm"):
            value, unit = value * 1000, "mg"
        if unit == "µg":
            unit = "mcg"
        found.add((value.normalize(), unit))
    return found


def _form(text: str) -> str | None:
    words = set(_key(text).split())
    return next((form for form, hints in _FORMS.items() if words & set(hints)), None)


def _medicine_words(text: str) -> str:
    stripped = _STRENGTH.sub(" ", text)
    hints = {h for hints in _FORMS.values() for h in hints}
    return " ".join(w for w in _key(stripped).split() if w not in hints and not w.isdigit())


async def check_drug(session: AsyncSession, name: str, unit_price: Decimal | None) -> DrugCheck:
    medicine = _medicine_words(name)
    words = [w for w in medicine.split() if len(w) >= 4]
    result = DrugCheck(name=name, unit_price=unit_price, matches=[])
    if not words:
        return result
    rows = list(
        await session.scalars(
            select(DrugPriceRow)
            .where(or_(*(DrugPriceRow.medicine.ilike(f"%{w}%") for w in words)))
            .limit(ROWS_PER_NAME)
        )
    )
    scored = [(float(fuzz.token_set_ratio(medicine, _key(r.medicine))), r) for r in rows]
    best = max((s for s, _ in scored), default=0.0)
    same = [r for s, r in scored if s >= DRUG_NAME_SCORE and s == best]
    form, strengths = _form(name), _strengths(name)
    fitting = [
        r
        for r in same
        if (form is None or form in r.dosage_form.lower())
        and strengths <= _strengths(r.dosage_form)
    ]
    chosen = fitting or same
    result.matches = [
        DrugPrice(
            medicine=r.medicine,
            dosage_form=r.dosage_form,
            unit=r.unit,
            ceiling_price=r.ceiling_price,
            notification=r.notification,
            page=r.page,
        )
        for r in sorted(chosen, key=lambda r: r.serial)
    ]
    prices = {r.ceiling_price for r in fitting}
    if strengths and len(prices) == 1:
        result.ceiling_price = prices.pop()
        if unit_price is not None:
            result.over_ceiling = unit_price > result.ceiling_price
    nlem = list(
        await session.scalars(
            select(NlemMedicineRow.medicine)
            .where(or_(*(NlemMedicineRow.medicine.ilike(f"%{w}%") for w in words)))
            .limit(ROWS_PER_NAME)
        )
    )
    hit = max(nlem, default=None, key=lambda m: fuzz.token_set_ratio(medicine, _key(m)))
    if hit is not None and fuzz.token_set_ratio(medicine, _key(hit)) >= DRUG_NAME_SCORE:
        result.nlem, result.nlem_medicine = True, hit
    return result


# --- Non-payable and exclusions -----------------------------------------------------------


async def active_non_payable(session: AsyncSession) -> list[NonPayableItemRow]:
    """The loaded wording's lists when one is loaded, else the fixture."""
    loaded = await session.scalar(
        select(NonPayableItemRow.source_version)
        .where(NonPayableItemRow.source != FIXTURE)
        .order_by(NonPayableItemRow.id.desc())
        .limit(1)
    )
    query: Any = select(NonPayableItemRow)
    if loaded is None:
        query = query.where(NonPayableItemRow.source == FIXTURE)
    else:
        query = query.where(
            NonPayableItemRow.source != FIXTURE, NonPayableItemRow.source_version == loaded
        )
    return list(await session.scalars(query))


async def match_non_payable(session: AsyncSession, text: str) -> list[NonPayableCandidate]:
    key = _key(text)
    if not key:
        return []
    scored = []
    for row in await active_non_payable(session):
        item = _key(row.item)
        score = float(fuzz.token_set_ratio(key, item))
        if score >= NON_PAYABLE_SCORE:
            scored.append((score, float(fuzz.ratio(key, item)), row))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2].list_name, s[2].item_number))
    return [
        NonPayableCandidate(
            list_name=row.list_name,
            item_number=row.item_number,
            item=row.item,
            condition=row.condition,
            payable_in_product=row.payable_in_product,
            score=score,
        )
        for score, _, row in scored[:MAX_NON_PAYABLE_CANDIDATES]
    ]


async def exclusion(session: AsyncSession, code: str) -> Exclusion | None:
    text = code.strip()
    standard = re.match(r"(?i)^excl?\s*0*(\d{1,2})$", text)
    key = f"Excl{int(standard[1]):02d}" if standard else text.upper()
    rows = list(await session.scalars(select(ExclusionRow).where(ExclusionRow.code == key)))
    if not rows:
        return None
    row = min(rows, key=lambda r: (r.scope != "IRDAI", -r.id))
    return Exclusion(
        scope=row.scope,
        code=row.code,
        title=row.title,
        summary=row.summary,
        page=row.page,
        source=row.source,
    )


# --- Hospital agreements and the package crosswalk ----------------------------------------


async def hospital_agreement(
    session: AsyncSession, hospital_id: str, as_of: date | None
) -> HospitalAgreement | None:
    query = select(HospitalAgreementRow).where(HospitalAgreementRow.hospital_id == hospital_id)
    if as_of is not None:
        query = query.where(
            HospitalAgreementRow.valid_from <= as_of,
            or_(HospitalAgreementRow.valid_to.is_(None), HospitalAgreementRow.valid_to >= as_of),
        )
    row = await session.scalar(query.order_by(HospitalAgreementRow.valid_from.desc()).limit(1))
    if row is None:
        return None
    return HospitalAgreement(
        hospital_id=row.hospital_id,
        scheme=row.scheme,
        accreditation=row.accreditation,
        package_ward=row.package_ward,
        non_package_discount_percent=Decimal(row.non_package_discount_percent).quantize(CENTS),
        valid_from=row.valid_from,
        note=row.note,
        source=row.source,
    )


async def package_crosswalk(
    session: AsyncSession, scheme: str, package_code: str
) -> PackageCrosswalk | None:
    row = await session.scalar(
        select(PackageCrosswalkRow)
        .where(
            PackageCrosswalkRow.scheme == scheme.strip().upper(),
            PackageCrosswalkRow.package_code == package_code.strip().upper(),
        )
        .order_by(PackageCrosswalkRow.id.desc())
        .limit(1)
    )
    return None if row is None else _crosswalk(row)


async def packages_for_diagnoses(
    session: AsyncSession, scheme: str, codes: list[str]
) -> list[PackageCrosswalk]:
    """The latest crosswalk row of each package of the scheme, kept when its ranges hold
    any of the codes."""
    if not codes:
        return []
    rows = await session.scalars(
        select(PackageCrosswalkRow)
        .where(PackageCrosswalkRow.scheme == scheme.strip().upper())
        .order_by(PackageCrosswalkRow.package_code, PackageCrosswalkRow.id.desc())
    )
    latest: dict[str, PackageCrosswalkRow] = {}
    for row in rows:
        latest.setdefault(row.package_code, row)
    return [
        _crosswalk(row)
        for row in latest.values()
        if any(code_in_ranges(c, list(row.icd10cm or [])) for c in codes)
    ]


def _crosswalk(row: PackageCrosswalkRow) -> PackageCrosswalk:
    return PackageCrosswalk(
        scheme=row.scheme,
        package_code=row.package_code,
        icd10cm=list(row.icd10cm or []),
        guideline_code=row.guideline_code,
        note=row.note,
        source=row.source,
    )
