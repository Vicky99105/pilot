"""Knowledge base lookups over the public reference tables, through the contract, on a small
hand-made set of rows shaped like the loaders' output."""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    DrugPriceRow,
    ExclusionRow,
    GuidelineSectionRow,
    LosNormRow,
    NlemMedicineRow,
    NonPayableItemRow,
    PackageItemRow,
    PackageMasterRow,
    RateFactorRow,
)
from app.database.seed import seed_claims_db
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS

D = Decimal
SUNRISE_DELHI, LOTUS_GURUGRAM, GANGA_LUCKNOW = HOSPITAL_ROHINI_IDS[:3]


def _master(scheme: str, code: str, name: str, package: str | None, rate: str, **extra: object
            ) -> PackageMasterRow:  # fmt: skip
    return PackageMasterRow(
        scheme=scheme,
        version={"CGHS": "2025-10-13", "PMJAY": "HBP2022", "MEDISEP": "PhaseII-2026"}[scheme],
        package_code=extra.pop("package_code", code),
        procedure_code=code,
        name=name,
        package_name=package,
        search_text=" ".join(x for x in (package, name) if x).lower(),
        rate=D(rate),
        extra={},
        source=scheme.lower(),
        source_version="v",
        **extra,
    )


@pytest.fixture
async def kb(sessions: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    await seed_claims_db(sessions)
    async with sessions.begin() as session:
        cghs = _master("CGHS", "AG045", "Appendicectomy", None, "20900")
        hbp = _master("PMJAY", "SG017A", "Appendicectomy- Open", "Appendicectomy", "21700",
                      package_code="SG017", alos_days=3, multiple_procedures=True)  # fmt: skip
        medisep = _master("MEDISEP", "MS1504", "Lap", "Appendicectomy", "21150", alos_days=4)
        session.add_all([cghs, hbp, medisep])
        await session.flush()
        for tier, factor in (("X", 1), ("Y", D("0.9")), ("Z", D("0.8"))):
            for accreditation, a in (("non_nabh", D("0.85")), ("nabh", 1)):
                session.add(
                    PackageItemRow(package_id=cghs.id, tier=tier, accreditation=accreditation,
                                   ward="semi_private", rate=D(20900) * factor * a)
                )  # fmt: skip
        session.add_all(
            [
                RateFactorRow(scheme="CGHS", version="2025-10-13", factor="city_tier", key=city,
                              text_value=tier, source="cghs", source_version="v")
                for city, tier in (("delhi", "X"), ("gurgaon", "X"), ("lucknow", "Y"), ("*", "Z"))
            ]
        )  # fmt: skip
        session.add_all(
            [
                LosNormRow(scheme="MEDISEP", version="PhaseII-2026", package_code="MS1504",
                           name="Lap", los_days=4, los_text="4", source="medisep",
                           source_version="v"),
                LosNormRow(scheme="PMJAY", version="STG", package_code="SG017A",
                           name="Appendicectomy Open", los_days=3, los_text="3 days",
                           source="stg", source_version="v"),
                GuidelineSectionRow(package_code="SG017A", old_code="SG017A",
                                    procedure_name="Appendicectomy Open",
                                    section="claim_documents",
                                    items=["Indoor case papers", "Histopathology report"],
                                    match_score=100.0, file_name="stg.pdf", page=120,
                                    source="stg", source_version="v"),
                GuidelineSectionRow(package_code="SG017A", old_code="SG017A",
                                    procedure_name="Appendicectomy Open",
                                    section="preauth_documents", items=["Clinical notes"],
                                    match_score=100.0, file_name="stg.pdf", page=120,
                                    source="stg", source_version="v"),
                DrugPriceRow(serial=159, medicine="Ceftriaxone",
                             dosage_form="Powder for Injection 1000mg", unit="1 Vial",
                             ceiling_price=D("70.68"), notification="S.O. 1575(E)",
                             effective_from=__import__("datetime").date(2026, 4, 1), page=35,
                             source="drugs", source_version="v"),
                DrugPriceRow(serial=158, medicine="Ceftriaxone",
                             dosage_form="Powder for Injection 500mg", unit="1 Vial",
                             ceiling_price=D("37.97"), notification="S.O. 1575(E)",
                             effective_from=__import__("datetime").date(2026, 4, 1), page=35,
                             source="drugs", source_version="v"),
                NlemMedicineRow(number="6.2.1.4", section="6 Anti-infective",
                                medicine="Ceftriaxone", levels="P,S,T",
                                formulations=["Powder for Injection 1 g"], page=21,
                                source="nlem", source_version="v"),
                NonPayableItemRow(list_name="I", item_number=56, item="GLOVES",
                                  condition="Sterile gloves are paid; others are not",
                                  payable_in_product="not_payable_unless", page=21,
                                  source="non_payable", source_version="W"),
                NonPayableItemRow(list_name="II", item_number=27, item="ADMISSION KIT",
                                  condition="Part of the room charges",
                                  payable_in_product="not_payable", page=22,
                                  source="non_payable", source_version="W"),
                ExclusionRow(scope="IRDAI", code="Excl03", title="First 30 days",
                             summary="Illness in the first 30 days is excluded.", page=7,
                             source="exclusions", source_version="v"),
            ]
        )  # fmt: skip
    return DefaultKnowledgeBase(sessions)


async def test_tariff_candidates_by_code_carry_the_rate_for_the_hospitals_city_tier(
    kb: KnowledgeBase,
) -> None:
    [delhi] = await kb.tariff_candidates(["AG045"], [], SUNRISE_DELHI)
    [lucknow] = await kb.tariff_candidates(["ag045"], [], GANGA_LUCKNOW)

    assert (delhi.scheme, delhi.matched_on, delhi.tier, delhi.rate) == ("CGHS", "code", "X",
                                                                        D("20900.00"))  # fmt: skip
    assert (lucknow.tier, lucknow.rate) == ("Y", D("18810.00"))
    assert {(r.accreditation, r.rate) for r in lucknow.rates} == {
        ("non_nabh", D("15988.50")),
        ("nabh", D("18810.00")),
    }


async def test_gurugram_is_rated_as_x_after_the_tier_om(kb: KnowledgeBase) -> None:
    [candidate] = await kb.tariff_candidates(["AG045"], [], LOTUS_GURUGRAM)

    assert candidate.tier == "X"


async def test_tariff_candidates_by_name_search_package_and_procedure_names(
    kb: KnowledgeBase,
) -> None:
    found = await kb.tariff_candidates([], ["Laparoscopic appendicectomy"], None)

    assert {(c.scheme, c.procedure_code) for c in found} >= {
        ("PMJAY", "SG017A"),
        ("MEDISEP", "MS1504"),
    }
    assert all(c.matched_on == "name" for c in found)
    hbp = next(c for c in found if c.scheme == "PMJAY")
    assert (hbp.rate, hbp.alos_days, hbp.multiple_procedures) == (D("21700.00"), 3, True)


async def test_guideline_sections_come_by_package_code(kb: KnowledgeBase) -> None:
    sections = await kb.get_guideline("SG017A")

    assert [s.section for s in sections] == ["claim_documents", "preauth_documents"]
    assert sections[0].items == ["Indoor case papers", "Histopathology report"]
    assert (sections[0].page, sections[0].file_name) == (120, "stg.pdf")
    assert await kb.get_guideline("ZZ999Z") == []


async def test_los_norm_by_code_then_by_name(kb: KnowledgeBase) -> None:
    by_code = await kb.get_los_norm("MS1504")
    by_name = await kb.get_los_norm("appendicectomy open")

    assert by_code is not None and (by_code.los_days, by_code.scheme) == (4, "MEDISEP")
    assert by_name is not None and (by_name.package_code, by_name.los_days) == ("SG017A", 3)
    assert await kb.get_los_norm("heart transplant") is None


async def test_check_drug_finds_the_formulation_and_compares_the_unit_price(
    kb: KnowledgeBase,
) -> None:
    over = await kb.check_drug("Inj. Ceftriaxone 1 g", D("95.00"))
    under = await kb.check_drug("Ceftriaxone 500 mg injection", D("30"))
    unknown = await kb.check_drug("Wonder tonic", D("10"))

    assert (over.ceiling_price, over.over_ceiling, over.nlem) == (D("70.6800"), True, True)
    assert (under.ceiling_price, under.over_ceiling) == (D("37.9700"), False)
    assert (unknown.matches, unknown.over_ceiling, unknown.nlem) == ([], None, False)


async def test_check_drug_with_no_strength_lists_every_formulation(kb: KnowledgeBase) -> None:
    result = await kb.check_drug("Ceftriaxone", None)

    assert len(result.matches) == 2
    assert (result.ceiling_price, result.over_ceiling) == (None, None)


async def test_match_non_payable_ranks_list_items_against_a_bill_line(kb: KnowledgeBase) -> None:
    found = await kb.match_non_payable("Surgical gloves (non sterile) x 12")

    assert found[0].item == "GLOVES"
    assert found[0].payable_in_product == "not_payable_unless"
    assert found[0].score >= 70
    assert await kb.match_non_payable("Inj. Ceftriaxone 1 g") == []


async def test_exclusion_by_code(kb: KnowledgeBase) -> None:
    excl = await kb.exclusion("excl03")

    assert excl is not None and (excl.code, excl.title, excl.page) == ("Excl03", "First 30 days", 7)
    assert await kb.exclusion("Excl99") is None


# --- Slice P2-4: hospital agreements and the diagnosis-to-package crosswalk -------------


async def test_every_seeded_hospital_has_an_agreement(kb: KnowledgeBase) -> None:
    agreements = {h: await kb.hospital_agreement(h) for h in HOSPITAL_ROHINI_IDS}

    sunrise = agreements[SUNRISE_DELHI]
    assert sunrise is not None
    assert (sunrise.scheme, sunrise.accreditation, sunrise.package_ward) == (
        "CGHS",
        "nabh",
        "semi_private",
    )
    assert sunrise.non_package_discount_percent == D("10.00")
    assert all(a is not None for a in agreements.values())
    nursing_home = agreements[HOSPITAL_ROHINI_IDS[3]]
    assert nursing_home is not None and nursing_home.scheme == "OWN"
    assert nursing_home.non_package_discount_percent == D("0.00")
    assert await kb.hospital_agreement("0000000000000") is None


async def test_packages_for_diagnoses_gives_the_schemes_packages_that_treat_them(
    kb: KnowledgeBase,
) -> None:
    pmjay = await kb.packages_for_diagnoses("pmjay", ["K35.80"])
    cghs = await kb.packages_for_diagnoses("CGHS", ["E11.9", "K35.32"])

    assert [p.package_code for p in pmjay] == ["SG017A", "SG017B"]
    assert [p.package_code for p in cghs] == ["AG045", "AG095"]
    assert await kb.packages_for_diagnoses("PMJAY", ["E11.9"]) == []
    assert await kb.packages_for_diagnoses("PMJAY", []) == []


async def test_package_crosswalk_gives_the_diagnoses_and_the_guideline_code(
    kb: KnowledgeBase,
) -> None:
    lap = await kb.package_crosswalk("CGHS", "AG095")
    unknown = await kb.package_crosswalk("CGHS", "AG001")

    assert lap is not None
    assert (lap.icd10cm, lap.guideline_code) == (["K35-K38"], "SG017B")
    assert unknown is None
