"""A small hand-made set of reference rows shaped like the loaders' output, for the package
and the laparoscopic appendicectomy the tracer claim uses. Values copy the loaded sources:
CGHS 2025 AG095 (NABH, X tier, semi-private Rs 33,000), HBP 2022 SG017B, the STG for
SG017B, and NPPA ceiling prices (S.O. 1575(E)) for three of the tracer's medicines."""

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    DrugPriceRow,
    GuidelineSectionRow,
    NonPayableItemRow,
    PackageItemRow,
    PackageMasterRow,
    RateFactorRow,
)

D = Decimal
VERSIONS = {"CGHS": "2025-10-13", "PMJAY": "HBP2022", "MEDISEP": "PhaseII-2026"}

# The guideline's mandatory claim documents, as the STG compilation writes them.
SG017B_CLAIM_DOCUMENTS = [
    "Is there documentary evidence of indication of surgery? Yes",
    "Are detailed operative notes available with indications for surgery and",
    "Is discharge summary available with follow-up advise at the time of discharge? Yes",
    "Is the picture(s) of gross specimen removed available (except in operative drainage",
    "Is the Histopathology report of the specimen removed available (except in",
    "Is Pre-anesthesia check-up report available? Yes",
]


def _master(scheme: str, code: str, name: str, rate: str, **extra: object) -> PackageMasterRow:
    package = extra.pop("package_code", code)
    return PackageMasterRow(
        scheme=scheme,
        version=VERSIONS[scheme],
        package_code=package,
        procedure_code=code,
        name=name,
        search_text=name.lower(),
        rate=D(rate),
        extra={},
        source=scheme.lower(),
        source_version="v",
        **extra,
    )


async def add_reference_rows(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        lap = _master("CGHS", "AG095", "Laparoscopic Appendectomy", "33000")
        open_ = _master("CGHS", "AG045", "Appendicectomy", "20900")
        chole = _master("CGHS", "AG094", "Laparoscopic Cholecystectomy", "36300")
        hbp = _master("PMJAY", "SG017B", "Lap- Appendicectomy", "21700", package_code="SG017",
                      alos_days=3, multiple_procedures=True)  # fmt: skip
        session.add_all([lap, open_, chole, hbp])
        await session.flush()
        for row, base in ((lap, D(33000)), (open_, D(20900)), (chole, D(36300))):
            for tier, t in (("X", D(1)), ("Y", D("0.9")), ("Z", D("0.8"))):
                for accreditation, a in (("non_nabh", D("0.85")), ("nabh", D(1))):
                    session.add(
                        PackageItemRow(package_id=row.id, tier=tier, accreditation=accreditation,
                                       ward="semi_private", rate=base * t * a)
                    )  # fmt: skip
        session.add_all(
            [
                RateFactorRow(scheme="CGHS", version="2025-10-13", factor="city_tier", key=city,
                              text_value=tier, source="cghs", source_version="v")
                for city, tier in (("delhi", "X"), ("gurgaon", "X"), ("pune", "X"),
                                   ("lucknow", "Y"), ("*", "Z"))
            ]
        )  # fmt: skip
        session.add_all(
            [
                GuidelineSectionRow(package_code="SG017B", old_code="SG017B",
                                    procedure_name="Appendicectomy", section=section,
                                    items=items, match_score=100.0, file_name="stg.pdf",
                                    page=36, source="stg", source_version="v")
                for section, items in (
                    ("claim_documents", SG017B_CLAIM_DOCUMENTS),
                    ("alos", ["3 Days"]),
                    ("clinical_pointers", ["Pain shifting to the lower right abdomen"]),
                    ("doctor_qualification", ["MS General Surgery"]),
                )
            ]
        )  # fmt: skip
        session.add_all(
            [
                DrugPriceRow(serial=serial, medicine=medicine, dosage_form=form, unit=unit,
                             ceiling_price=D(price), notification="S.O. 1575(E)",
                             effective_from=date(2026, 4, 1), page=35, source="drugs",
                             source_version="v")
                for serial, medicine, form, unit, price in (
                    (159, "Ceftriaxone", "Powder for Injection 1000mg", "1 Vial", "63.88"),
                    (300, "Metronidazole", "Injection 500mg/100mL", "1 ML", "0.21"),
                    (500, "Tramadol", "Injection 50mg/mL (upto 2ml pack)", "1 ML", "12.07"),
                )
            ]
        )  # fmt: skip


# The wording's non-payable items the tracer bill comes near, as the non_payable loader
# writes them (tests/pg checks each against the loaded table): gloves, which R11 matches,
# and the three nursing items "Nursing Care Charges" is near, which go to R12.
WORDING = "GODHLIP24179V022324"
TRACER_WORDING_ITEMS: list[tuple[str, int, str, str, str]] = [
    ("I", 21, "SERVICE CHARGES WHERE NURSING CHARGE ALSO CHARGED",
     "Not paid when nursing is also charged", "not_payable"),
    ("I", 52, "PRIVATE NURSES CHARGES- SPECIAL NURSING CHARGES",
     "Special nursing during the stay is paid; nursing after discharge is not",
     "payable_within_limits"),
    ("I", 56, "GLOVES",
     "Sterile gloves are paid; non-sterile (examination) gloves are not, so a bill line must "
     "show the gloves were sterile", "not_payable_unless"),
    ("II", 38, "Nursing, DMO/ RMO charges included in room rent under associated medical "
     "expenses", "Part of the room charges; not paid as a separate item", "not_payable"),
]  # fmt: skip


async def add_tracer_wording_items(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Load the wording's items above as the active non-payable lists (they replace the
    fixture), as in the acceptance environment."""
    async with sessions.begin() as session:
        session.add_all(
            NonPayableItemRow(
                list_name=list_name,
                item_number=number,
                item=item,
                condition=condition,
                payable_in_product=flag,
                source="non_payable",
                source_version=WORDING,
            )
            for list_name, number, item, condition, flag in TRACER_WORDING_ITEMS
        )
