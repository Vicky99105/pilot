"""The public reference loaders on the full downloaded sources, on real Postgres: counts in
plausible ranges, known entries, the tracer's deductions against the full non-payable lists,
the tracer's package, guideline, norm and drug prices against the loaded sources, the
waiting-period and exclusion ICD mapping against the FY2027 release, and every new lookup
under the 50 ms budget.

Needs PG_TEST_DATABASE_URL and the source inventory in KB_REFERENCE_DIR (default
.local/kb_sources). Skipped when either is missing. Everything is loaded once for the module.
"""

import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.models import NonPayableItemRow
from app.database.seed import seed_claims_db
from app.knowledge.loaders.cghs import load_cghs
from app.knowledge.loaders.common import LoadReport
from app.knowledge.loaders.drugs import load_drugs
from app.knowledge.loaders.exclusions import load_exclusions
from app.knowledge.loaders.hbp import load_hbp
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.knowledge.loaders.medisep import load_medisep
from app.knowledge.loaders.non_payable import load_non_payable
from app.knowledge.loaders.policy_wording import load_policy_wording
from app.knowledge.loaders.stg import load_stg
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.rules.contract import NonPayableItem
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS, PRODUCT_UIN
from tests.pg.conftest import ALEMBIC_INI, PG_ENV, _server
from tests.services.reference_rows import TRACER_WORDING_ITEMS
from tools.mockgen import tracer_claim as gen

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = Path(os.environ.get("KB_REFERENCE_DIR", str(ROOT / ".local" / "kb_sources")))
ICD_DIR = SOURCE_DIR / "icd-2027"
BUDGET_MS = 50.0
RUNS = 7
D = Decimal

if not (SOURCE_DIR / "MANIFEST.json").is_file():
    pytestmark = [pytest.mark.pg, pytest.mark.skip(reason=f"no source inventory at {SOURCE_DIR}")]

ORDER = ["non_payable", "policy_wording", "exclusions", "cghs", "hbp", "medisep", "drugs", "stg"]
LOADERS: dict[str, Callable[[async_sessionmaker[AsyncSession], Path], Awaitable[LoadReport]]] = {
    "non_payable": load_non_payable,
    "policy_wording": load_policy_wording,
    "exclusions": load_exclusions,
    "cghs": load_cghs,
    "hbp": load_hbp,
    "medisep": load_medisep,
    "drugs": load_drugs,
    "stg": load_stg,
}


class Loaded:
    def __init__(self, url: str, reports: dict[str, LoadReport]) -> None:
        self.url, self.reports = url, reports


@pytest.fixture(scope="module")
def loaded() -> Iterator[Loaded]:
    if not os.environ.get(PG_ENV):
        pytest.skip(f"{PG_ENV} is not set")
    name = f"p23_{uuid4().hex[:12]}"
    admin = create_engine(_server(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _server().set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = url
        command.upgrade(Config(str(ALEMBIC_INI)), "head")

        async def load() -> dict[str, LoadReport]:
            engine = create_async_engine(url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                await seed_claims_db(sessions)  # the fixture lists, replaced by the load
                reports = {}
                if ICD_DIR.is_dir():
                    reports["icd10cm"] = await load_icd10cm(sessions, ICD_DIR)
                    reports["icd10pcs"] = await load_icd10pcs(sessions, ICD_DIR)
                for loader in ORDER:
                    reports[loader] = await LOADERS[loader](sessions, SOURCE_DIR)
                return reports
            finally:
                await engine.dispose()

        reports = asyncio.run(load())
        for loader in ORDER:
            print("\n".join(reports[loader].lines()))
        yield Loaded(url, reports)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _run(loaded: Loaded, work: Callable[[KnowledgeBase], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        engine = create_async_engine(loaded.url)
        try:
            return await work(DefaultKnowledgeBase(async_sessionmaker(engine)))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _query(loaded: Loaded, sql: str) -> list[Any]:
    engine = create_engine(loaded.url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql)))
    finally:
        engine.dispose()


# --- Counts and known entries -----------------------------------------------------------


def test_counts_are_in_plausible_ranges(loaded: Loaded) -> None:
    r = loaded.reports
    assert r["non_payable"].counts == {
        **r["non_payable"].counts,
        "List I": 68,
        "List II": 38,
        "List III": 23,
        "List IV": 18,
    }
    assert r["policy_wording"].counts["clauses"] >= 30
    assert r["policy_wording"].counts["specified conditions"] == 21
    assert r["exclusions"].counts == {"standard exclusions": 18, "specific exclusions": 5}
    assert r["cghs"].counts["package masters"] == 1998
    assert r["cghs"].counts["package items"] == 1998 * 9
    assert 2400 <= r["hbp"].counts["package rows"] <= 2700
    assert 1900 <= r["hbp"].counts["unique procedure codes"] <= 2100
    assert 2400 <= r["medisep"].counts["packages"] <= 2600
    assert r["drugs"].counts["NPPA formulations"] == 767
    assert 380 <= r["drugs"].counts["NLEM medicines"] <= 500
    assert 900 <= r["stg"].counts["compilation procedures"] <= 1200
    assert r["stg"].counts["STG PDFs"] == 50


def test_the_wording_agrees_with_the_product_config(loaded: Loaded) -> None:
    checks = loaded.reports["policy_wording"].checks
    limits = {k: v for k, v in checks.items() if k.startswith(PRODUCT_UIN) and ": " in v}

    assert len(limits) >= 12
    differs = [f"{k}: {v}" for k, v in limits.items() if "DIFFERS" in v or "UNVERIFIED" in v]
    assert differs == []
    assert checks[f"{PRODUCT_UIN} in the wording, not in the config"] == "none"


def test_known_entries_are_loaded(loaded: Loaded) -> None:
    cghs = _query(
        loaded,
        "SELECT i.tier, i.accreditation, i.rate FROM package_items i JOIN package_masters m "
        "ON m.id = i.package_id WHERE m.scheme = 'CGHS' AND m.procedure_code = 'AG045'",
    )
    rates = {(t, a): r for t, a, r in cghs}
    assert (rates[("X", "nabh")], rates[("Y", "nabh")], rates[("Z", "nabh")]) == (
        D("20900.00"),
        D("18810.00"),
        D("16720.00"),
    )
    assert rates[("X", "non_nabh")] == D("17765.00")

    [hbp] = _query(
        loaded,
        "SELECT name, rate, alos_days FROM package_masters WHERE scheme = 'PMJAY' "
        "AND procedure_code = 'SG017A' AND specialty_code = 'SG'",
    )
    assert hbp == ("Appendicectomy- Open", D("21700.00"), 3)

    [medisep] = _query(
        loaded,
        "SELECT name, los_days FROM los_norms WHERE scheme = 'MEDISEP' AND package_code = 'MS1421'",
    )
    assert medisep == ("Pancreatic Necrosectomy", 6)

    [excl] = _query(loaded, "SELECT title, page FROM exclusions WHERE code = 'Excl03'")
    assert excl == ("First 30 days", 7)

    paracetamol = _query(
        loaded,
        "SELECT dosage_form, unit, ceiling_price FROM drug_prices "
        "WHERE medicine = 'Paracetamol' AND dosage_form = 'Tablet 500mg'",
    )
    assert paracetamol == [("Tablet 500mg", "1 Tablet", D("0.9300"))]

    [gloves] = _query(
        loaded,
        "SELECT list_name, item_number, payable_in_product, page FROM non_payable_items "
        "WHERE item = 'GLOVES'",
    )
    assert gloves == ("I", 56, "not_payable_unless", 21)
    assert _query(loaded, "SELECT count(*) FROM non_payable_items WHERE source = 'fixture'") == [
        (0,)
    ]


def test_the_city_tier_om_is_applied(loaded: Loaded) -> None:
    tiers = dict(
        _query(
            loaded,
            "SELECT key, text_value FROM rate_factors WHERE scheme = 'CGHS' "
            "AND factor = 'city_tier' AND key IN ('delhi', 'gurgaon', 'noida', 'lucknow', "
            "'goa', '*')",
        )
    )
    assert tiers == {"delhi": "X", "gurgaon": "X", "noida": "X", "lucknow": "Y", "goa": "Y",
                     "*": "Z"}  # fmt: skip


# --- The tracer claim against the full lists --------------------------------------------


def test_the_tracer_ground_truth_holds_against_the_full_non_payable_lists(loaded: Loaded) -> None:
    rows = _query(
        loaded,
        "SELECT list_name, item_number, item, condition, payable_in_product "
        "FROM non_payable_items WHERE source = 'non_payable'",
    )
    full = [
        NonPayableItem(list_name=a, item_number=b, item=c, condition=d, payable_in_product=e)
        for a, b, c, d, e in rows
    ]
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    claim = gen.TracerClaim(admission=date(2026, 10, 29))
    descriptions = sorted({i.description for i in claim.items})

    async def unclear(kb: KnowledgeBase) -> set[str]:
        return {d for d in descriptions if await kb.match_non_payable(d)}

    # The lines bill review sends to R12 against the loaded lists are exactly the ones the
    # generator reads, and each reading is a loaded item as written. Gloves matches R11
    # itself and is never unclear.
    near = _run(loaded, unclear) - {gen.NON_PAYABLE_ITEM}
    print(f"\nTracer lines near a list item (R12): {sorted(near)}")
    assert near == set(gen.R12_READINGS)
    for reading in gen.R12_READINGS.values():
        assert reading is None or reading in full
    # The wording rows the workflow test loads are the loaded rows.
    for list_name, number, item, condition, flag in TRACER_WORDING_ITEMS:
        assert NonPayableItem(list_name=list_name, item_number=number, item=item,
                              condition=condition, payable_in_product=flag) in full  # fmt: skip

    fixture_sheet = gen.expected_sheet(claim, rules)
    full_sheet = gen.expected_sheet(claim, rules, full)

    deducted = [
        (ln["description"], ln["deducted"], ln["rule_id"]) for ln in full_sheet["deductions"]
    ]
    assert ("Gloves", D("264.00"), "R11") in deducted
    assert ("Nursing Care Charges", D("900.00"), "R12") in deducted
    assert [ln["deducted"] for ln in full_sheet["lines"]] == [
        ln["deducted"] for ln in fixture_sheet["lines"]
    ]
    assert full_sheet["payable"] == fixture_sheet["payable"] == D("60644.52")


# --- Slice P2-4: the tracer's package, guideline, norm and drug prices ---------------------


def test_the_tracer_package_and_rate_come_from_the_agreement_and_the_cghs_list(
    loaded: Loaded,
) -> None:
    async def go(kb: KnowledgeBase) -> Any:
        agreement = await kb.hospital_agreement(gen.HOSPITAL_ROHINI)
        found = await kb.tariff_candidates([], [gen.PROCEDURE], gen.HOSPITAL_ROHINI)
        return agreement, found

    agreement, found = _run(loaded, go)
    assert agreement is not None
    assert (agreement.scheme, agreement.accreditation, agreement.package_ward) == (
        gen.PACKAGE.scheme, "nabh", "semi_private",
    )  # fmt: skip
    assert agreement.non_package_discount_percent == gen.NON_PACKAGE_DISCOUNT
    [ag095] = [c for c in found if c.scheme == "CGHS" and c.procedure_code == "AG095"]
    print(f"\nCandidates for {gen.PROCEDURE!r}: "
          f"{[(c.scheme, c.procedure_code, c.name, str(c.rate)) for c in found]}")  # fmt: skip
    assert ag095.tier == "X"  # Delhi
    [rate] = [r.rate for r in ag095.rates
              if (r.accreditation, r.ward) == ("nabh", "semi_private")]  # fmt: skip
    assert rate == gen.PACKAGE.rate


def test_the_tracer_guideline_and_norm_come_through_our_crosswalk(loaded: Loaded) -> None:
    async def go(kb: KnowledgeBase) -> Any:
        crosswalk = await kb.package_crosswalk(gen.PACKAGE.scheme, gen.PACKAGE.package_code)
        assert crosswalk is not None and crosswalk.guideline_code is not None
        sections = await kb.get_guideline(crosswalk.guideline_code)
        norm = await kb.get_los_norm(crosswalk.guideline_code)
        return crosswalk, sections, norm

    crosswalk, sections, norm = _run(loaded, go)
    assert crosswalk.guideline_code == gen.GUIDELINE_CODE
    assert crosswalk.icd10cm == ["K35-K38"]
    names = {s.section for s in sections}
    assert {"claim_documents", "clinical_pointers"} <= names
    assert norm is not None and norm.los_days == gen.LOS_NORM_DAYS


def test_the_tracer_drug_ceilings_are_the_loaded_nppa_prices(loaded: Loaded) -> None:
    claim = gen.TracerClaim(admission=date(2026, 10, 29))
    rates = {i.description: i.rate for i in claim.items}

    async def go(kb: KnowledgeBase) -> dict[str, Any]:
        return {d: await kb.check_drug(d, rates[d]) for d in gen.DRUG_CEILINGS}

    checks = _run(loaded, go)
    for description, expected in gen.DRUG_CEILINGS.items():
        check = checks[description]
        [match] = [m for m in check.matches if m.ceiling_price == check.ceiling_price]
        assert (match.medicine, match.dosage_form, match.unit) == (
            expected.medicine, expected.dosage_form, expected.unit,
        ), description  # fmt: skip
        assert check.ceiling_price == expected.ceiling_price, description


def test_every_pcs_prefix_and_icd_range_the_product_config_names_exists(loaded: Loaded) -> None:
    """R9, R16 and R17 read ICD ranges and PCS prefixes from the product config. Each must
    name something in the FY2027 release."""
    if "icd10cm" not in loaded.reports:
        pytest.skip(f"no ICD release at {ICD_DIR}")
    product = DefaultRulesEngine(Settings().config_dir / "products").product(PRODUCT_UIN)

    async def go(kb: KnowledgeBase) -> list[str]:
        missing = []
        prefixes = [*product.cataract_pcs]
        prefixes += [p for t in product.modern_treatments for p in t.icd10pcs]
        prefixes += [p for e in product.exclusions for p in e.icd10pcs]
        for prefix in prefixes:
            valid = (
                await kb.pcs_validate(prefix)
                if len(prefix) == 7
                else bool(await kb.pcs_options(prefix))
            )
            if not valid:
                missing.append(f"PCS {prefix}")
        for exclusion in product.exclusions:
            for item in exclusion.icd10cm:
                for end in item.split("-") if "-" in item.strip("-") else [item]:
                    if await kb.get_icd_tabular(end.strip()) is None:
                        missing.append(f"{exclusion.code}: {end}")
        return missing

    assert _run(loaded, go) == []


# --- Waiting-period ICD mapping -----------------------------------------------------------


def test_every_waiting_period_range_names_codes_in_the_fy2027_release(loaded: Loaded) -> None:
    if "icd10cm" not in loaded.reports:
        pytest.skip(f"no ICD release at {ICD_DIR}")
    product = DefaultRulesEngine(Settings().config_dir / "products").product(PRODUCT_UIN)
    assert product.specific_waiting is not None
    cm = {c for (c,) in _query(loaded, "SELECT code FROM icd_tabular WHERE system = 'ICD-10-CM'")}
    tables = {
        c for (c,) in _query(loaded, "SELECT code FROM pcs_tables WHERE system = 'ICD-10-PCS'")
    }
    pcs_prefixes = {
        t + axes for t, axes in _query(
            loaded,
            "SELECT table_code, jsonb_array_elements(axes->0->1)->>0 FROM pcs_rows "
            "WHERE system = 'ICD-10-PCS'",
        )
    }  # fmt: skip
    missing = []
    print("\nWaiting-period mapping (Excl02):")
    for condition in product.specific_waiting.conditions:
        for item in condition.icd10cm:
            for end in item.split("-") if "-" in item.strip("-") else [item]:
                if end.strip() not in cm:
                    missing.append(f"{condition.item}: {end}")
        for prefix in condition.icd10pcs:
            if prefix not in tables and prefix not in pcs_prefixes:
                missing.append(f"{condition.item}: PCS {prefix}")
        print(
            f"  {condition.months}m {condition.item[:60]!r} CM={condition.icd10cm} "
            f"PCS={condition.icd10pcs} unmapped={condition.unmapped!r}"
        )
    assert missing == []


# --- Lookups under the budget -------------------------------------------------------------


def _median_ms(loaded: Loaded, call: Callable[[KnowledgeBase], Awaitable[Any]]) -> float:
    async def go(kb: KnowledgeBase) -> float:
        await call(kb)  # warm the connection and the plan
        timings = []
        for _ in range(RUNS):
            started = time.perf_counter()
            await call(kb)
            timings.append((time.perf_counter() - started) * 1000)
        return statistics.median(timings)

    result: float = _run(loaded, go)
    return result


@pytest.mark.parametrize(
    "name",
    [
        "tariff_by_code",
        "tariff_by_name",
        "guideline",
        "los_by_code",
        "los_by_name",
        "drug",
        "non_payable",
        "exclusion",
    ],
)
def test_each_lookup_runs_under_50_ms(loaded: Loaded, name: str) -> None:
    sunrise = HOSPITAL_ROHINI_IDS[0]
    calls: dict[str, Callable[[KnowledgeBase], Awaitable[Any]]] = {
        "tariff_by_code": lambda kb: kb.tariff_candidates(["AG045", "SG017A"], [], sunrise),
        "tariff_by_name": lambda kb: kb.tariff_candidates(
            [], ["Laparoscopic appendicectomy"], sunrise
        ),
        "guideline": lambda kb: kb.get_guideline("SG017A"),
        "los_by_code": lambda kb: kb.get_los_norm("MS1421"),
        "los_by_name": lambda kb: kb.get_los_norm("Laparoscopic cholecystectomy"),
        "drug": lambda kb: kb.check_drug("Inj. Ceftriaxone 1 g", D("68.50")),
        "non_payable": lambda kb: kb.match_non_payable("Gloves"),
        "exclusion": lambda kb: kb.exclusion("Excl03"),
    }
    median = _median_ms(loaded, calls[name])
    print(f"\n{name}: median {median:.1f} ms")
    assert median < BUDGET_MS


def test_lookups_answer_for_the_tracer_claim(loaded: Loaded) -> None:
    sunrise = HOSPITAL_ROHINI_IDS[0]

    async def go(kb: KnowledgeBase) -> dict[str, Any]:
        return {
            "tariff": await kb.tariff_candidates([], ["Laparoscopic appendicectomy"], sunrise),
            "guideline": await kb.get_guideline("SG017B"),
            "los": await kb.get_los_norm("SG017B"),
            "drug": await kb.check_drug("Inj. Ceftriaxone 1 g", D("68.50")),
            "gloves": await kb.match_non_payable("Gloves"),
        }

    found = _run(loaded, go)
    schemes = {(c.scheme, c.procedure_code) for c in found["tariff"]}
    print("\ntariff candidates:", [(c.scheme, c.procedure_code, c.name, c.rate) for c in
                                   found["tariff"]])  # fmt: skip
    print("drug check:", found["drug"].model_dump())
    assert ("PMJAY", "SG017B") in schemes
    assert {s.section for s in found["guideline"]} >= {"preauth_documents", "claim_documents"}
    assert found["los"] is not None and found["los"].los_days == 3
    assert found["drug"].nlem and found["drug"].matches
    assert found["gloves"][0].item == "GLOVES"


def test_seeding_after_the_load_adds_no_fixture_rows(loaded: Loaded) -> None:
    async def go() -> int:
        engine = create_async_engine(loaded.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_claims_db(sessions)
            async with sessions() as session:
                rows = await session.scalars(
                    select(NonPayableItemRow).where(NonPayableItemRow.source == "fixture")
                )
                return len(list(rows))
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == 0
