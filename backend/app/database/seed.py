"""Seed the claims database: Phase 1 users, mock hospitals and their agreements, the package
crosswalk, document checklists, policy clauses and non-payable items.

ICD codes are not seeded: the full CMS release is loaded with python -m app.knowledge.load.
The hand-made ICD-10-CM fixture (seed_icd_fixture) is for unit tests only.

Idempotent: rows are merged by primary key or natural key (checklists by request type and
document type, clauses by product and clause, non-payable items by list and number).
Running it twice changes nothing.
Run inside the app container: python -m app.database.seed
"""

import asyncio
from datetime import date
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.models import (
    DocumentChecklistRow,
    HospitalAgreementRow,
    HospitalRow,
    NonPayableItemRow,
    PackageCrosswalkRow,
    PolicyClauseRow,
    UserRoleRow,
    UserRow,
)
from app.database.seed_knowledge import NON_PAYABLE_ITEMS, POLICY_CLAUSES
from app.shared.seed_ids import (
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    HOSPITAL_ROHINI_IDS,
    HOSPITAL_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    OPS_USER_ID,
    PRODUCT_UIN,
    SENIOR_USER_ID,
)
from app.shared.types import DocType, RequestType, Role

RowT = TypeVar("RowT", UserRow, UserRoleRow, HospitalRow)

SOURCE = "pilot_seed"
SOURCE_VERSION = "2026-11-19"
FIXTURE = "fixture"  # a hand-made subset standing in for a full source load

USERS = [
    UserRow(
        id=MEMBER_USER_ID,
        name="Asha Verma (member)",
        kind="member",
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
        # Letters are addressed here. Reserved example domain: nothing can be delivered to it.
        email="asha.verma@member.example",
    ),
    UserRow(
        id=HOSPITAL_USER_ID,
        name="Sunrise Hospital desk",
        kind="hospital",
        hospital_id=HOSPITAL_ROHINI_IDS[0],
        email="desk@sunrise-hospital.example",
    ),
    UserRow(id=OPS_USER_ID, name="Ops", kind="staff"),
    UserRow(id=DOCTOR_USER_ID, name="Doctor", kind="staff"),
    UserRow(id=FINANCE_USER_ID, name="Finance", kind="staff"),
    UserRow(id=SENIOR_USER_ID, name="Senior approver", kind="staff"),
]

ROLES = [
    UserRoleRow(user_id=MEMBER_USER_ID, role=Role.MEMBER.value),
    UserRoleRow(user_id=HOSPITAL_USER_ID, role=Role.HOSPITAL_USER.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.CLAIMS_OPERATOR.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.BILL_PROCESSOR.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.INVESTIGATOR.value),
    UserRoleRow(user_id=DOCTOR_USER_ID, role=Role.MEDICAL_OFFICER.value),
    UserRoleRow(user_id=DOCTOR_USER_ID, role=Role.APPROVER.value, max_amount=Decimal("100000.00")),
    UserRoleRow(user_id=DOCTOR_USER_ID, role=Role.CRC_REVIEWER.value),
    UserRoleRow(user_id=FINANCE_USER_ID, role=Role.FINANCE.value),
    # Same role as the doctor's approver row, with no limit: approves above Rs 1 lakh.
    UserRoleRow(user_id=SENIOR_USER_ID, role=Role.APPROVER.value),
]

# (name, city, state, pincode, beds, differential billing). The nursing home charges one
# rate for every room category, so no proportionate deduction applies there.
_HOSPITALS = [
    ("Sunrise Multispeciality Hospital", "New Delhi", "Delhi", "110017", 120, True),
    ("Lotus Care Hospital", "Gurugram", "Haryana", "122001", 80, True),
    ("Ganga Medical Centre", "Lucknow", "Uttar Pradesh", "226010", 150, True),
    ("Sahyadri Nursing Home", "Pune", "Maharashtra", "411004", 40, False),
    ("Kaveri General Hospital", "Bengaluru", "Karnataka", "560034", 200, True),
]

HOSPITALS = [
    HospitalRow(
        rohini_id=rohini,
        name=name,
        city=city,
        state=state,
        pincode=pincode,
        network=True,
        differential_billing=differential,
        beds=beds,
        registered_on=date(2018, 4, 1),
        source=SOURCE,
        source_version=SOURCE_VERSION,
    )
    for rohini, (name, city, state, pincode, beds, differential) in zip(
        HOSPITAL_ROHINI_IDS, _HOSPITALS, strict=True
    )
]

# Mock agreements, one per hospital: (scheme, accreditation, ward packages are priced at,
# non-package discount %, valid from, note). Assumptions, stated so they can be checked:
# - CGHS hospitals take CGHS 2025 package rates (in force from 13.10.2025) for their city
#   tier, at their accreditation, for a semi-private ward: the ward the rate list is priced
#   at. A stay in a better room does not raise the package; the patient bears the difference.
# - Non-package charges carry a 10 to 12% discount on the hospital's own tariff, a typical
#   PPN figure. Medicines and implants are never discounted (product config).
# - Ganga prices packages from PM-JAY HBP 2022 at the base (general ward) rate.
# - The nursing home has no package agreement: its own tariff, no discount.
_AGREEMENTS = [
    ("CGHS", "nabh", "semi_private", "10", date(2025, 10, 13), "CGHS NABH rates, city tier X"),
    ("CGHS", "nabh", "semi_private", "12", date(2025, 10, 13), "CGHS NABH rates, city tier X"),
    ("PMJAY", None, "general", "8", date(2025, 4, 1), "PM-JAY HBP 2022 base rates"),
    ("OWN", None, None, "0", date(2024, 4, 1), "own tariff; no package agreement"),
    ("CGHS", "non_nabh", "semi_private", "10", date(2025, 10, 13),
     "CGHS non-NABH rates, city tier X"),
]  # fmt: skip

AGREEMENTS = [
    HospitalAgreementRow(
        hospital_id=rohini,
        scheme=scheme,
        accreditation=accreditation,
        package_ward=ward,
        non_package_discount_percent=Decimal(discount),
        valid_from=valid_from,
        note=note,
        source=SOURCE,
        source_version=SOURCE_VERSION,
    )
    for rohini, (scheme, accreditation, ward, discount, valid_from, note) in zip(
        HOSPITAL_ROHINI_IDS, _AGREEMENTS, strict=True
    )
]

# Our own crosswalk for the packages the mock claims use: (scheme, package, ICD-10-CM
# ranges it treats, the HBP 2022 code whose guideline and length of stay apply, note).
# K35-K38: acute and other appendicitis and other diseases of the appendix.
PACKAGE_CROSSWALK = [
    ("CGHS", "AG095", ["K35-K38"], "SG017B", "Laparoscopic appendectomy"),
    ("CGHS", "AG045", ["K35-K38"], "SG017A", "Open appendicectomy"),
    ("PMJAY", "SG017B", ["K35-K38"], "SG017B", "Lap appendicectomy"),
    ("PMJAY", "SG017A", ["K35-K38"], "SG017A", "Open appendicectomy"),
]


# Reimbursement checklist: (document, required, required only from this claimed amount, note).
# Documents marked not required are asked for only when they apply to the claim.
REIMBURSEMENT_CHECKLIST: list[tuple[DocType, bool, Decimal | None, str | None]] = [
    (DocType.CLAIM_FORM_PART_A, True, None, None),
    (DocType.CLAIM_FORM_PART_B, True, None, "Filled and sealed by the hospital"),
    (DocType.FINAL_BILL, True, None, "Itemised final bill"),
    (DocType.PAYMENT_RECEIPT, True, None, None),
    (DocType.DISCHARGE_SUMMARY, True, None, None),
    (DocType.INVESTIGATION_REPORT, False, None, "When investigations were billed"),
    (DocType.PRESCRIPTION, False, None, "When pharmacy was bought outside the hospital"),
    (DocType.PHARMACY_BILL, False, None, "When pharmacy was bought outside the hospital"),
    (DocType.CANCELLED_CHEQUE, True, None, "For payment to the member's account"),
    (DocType.KYC, True, Decimal("100000.00"), "Mandatory only when the claim is Rs 1 lakh or more"),
    (DocType.FIR_MLC, False, None, "When the stay was due to an injury or accident"),
]


async def seed_claims_db(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        for user in USERS:
            await session.merge(_copy(user))
        await session.flush()
        for role in ROLES:
            await session.merge(_copy(role))
        for hospital in HOSPITALS:
            await session.merge(_copy(hospital))
        await session.flush()
        await _seed_agreements(session)
        await _seed_crosswalk(session)
        await _seed_checklist(session, RequestType.REIMBURSEMENT, REIMBURSEMENT_CHECKLIST)
        await _seed_clauses(session)
        await _seed_non_payable(session)


async def _seed_agreements(session: AsyncSession) -> None:
    existing = {
        (row.hospital_id, row.valid_from): row
        for row in await session.scalars(select(HospitalAgreementRow))
    }
    for agreement in AGREEMENTS:
        row = existing.get((agreement.hospital_id, agreement.valid_from)) or HospitalAgreementRow(
            hospital_id=agreement.hospital_id, valid_from=agreement.valid_from
        )
        for column in ("scheme", "accreditation", "package_ward", "note", "source",
                       "source_version", "non_package_discount_percent"):  # fmt: skip
            setattr(row, column, getattr(agreement, column))
        session.add(row)


async def _seed_crosswalk(session: AsyncSession) -> None:
    existing = {
        (row.scheme, row.package_code): row
        for row in await session.scalars(
            select(PackageCrosswalkRow).where(PackageCrosswalkRow.source == SOURCE)
        )
    }
    for scheme, code, ranges, guideline, note in PACKAGE_CROSSWALK:
        row = existing.get((scheme, code)) or PackageCrosswalkRow(
            scheme=scheme, package_code=code, source=SOURCE
        )
        row.icd10cm, row.guideline_code, row.note = list(ranges), guideline, note
        row.source_version = SOURCE_VERSION
        session.add(row)


async def _seed_clauses(session: AsyncSession) -> None:
    existing = {
        row.clause_ref: row
        for row in await session.scalars(
            select(PolicyClauseRow).where(PolicyClauseRow.product_uin == PRODUCT_UIN)
        )
    }
    for clause_ref, title, text in POLICY_CLAUSES:
        row = existing.get(clause_ref) or PolicyClauseRow(
            product_uin=PRODUCT_UIN, clause_ref=clause_ref
        )
        row.title, row.text = title, text
        row.source, row.source_version = SOURCE, SOURCE_VERSION
        session.add(row)


async def _seed_non_payable(session: AsyncSession) -> None:
    """The fixture stands in for the lists until a wording is loaded; then it is not seeded."""
    loaded = await session.scalar(
        select(NonPayableItemRow.id).where(NonPayableItemRow.source != FIXTURE).limit(1)
    )
    if loaded is not None:
        return
    existing = {
        (row.list_name, row.item_number): row
        for row in await session.scalars(
            select(NonPayableItemRow).where(NonPayableItemRow.source == FIXTURE)
        )
    }
    for list_name, number, item, category, condition in NON_PAYABLE_ITEMS:
        row = existing.get((list_name, number)) or NonPayableItemRow(
            list_name=list_name, item_number=number
        )
        row.item, row.category, row.condition = item, category, condition
        row.source, row.source_version = FIXTURE, SOURCE_VERSION
        session.add(row)


async def _seed_checklist(
    session: AsyncSession,
    request_type: RequestType,
    items: list[tuple[DocType, bool, Decimal | None, str | None]],
) -> None:
    existing = {
        row.doc_type: row
        for row in await session.scalars(
            select(DocumentChecklistRow).where(
                DocumentChecklistRow.request_type == request_type.value
            )
        )
    }
    for doc_type, required, from_amount, notes in items:
        row = existing.get(doc_type.value) or DocumentChecklistRow(
            request_type=request_type.value, doc_type=doc_type.value
        )
        row.required, row.mandatory_from_amount, row.notes = required, from_amount, notes
        row.source, row.source_version = SOURCE, SOURCE_VERSION
        session.add(row)


def _copy(row: RowT) -> RowT:
    """A fresh instance per call, so module-level rows are never bound to a session.
    Unset columns are left out so merge keeps their stored or default values."""
    columns = {c.key: getattr(row, c.key) for c in row.__table__.columns}
    return type(row)(**{k: v for k, v in columns.items() if v is not None})


async def main() -> None:
    engine = create_async_engine(Settings().database_url)
    try:
        await seed_claims_db(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
    print(
        f"Seeded {len(USERS)} users, {len(ROLES)} roles, {len(HOSPITALS)} hospitals, "
        f"{len(AGREEMENTS)} hospital agreements, {len(PACKAGE_CROSSWALK)} crosswalk rows, "
        f"{len(REIMBURSEMENT_CHECKLIST)} reimbursement checklist items, "
        f"{len(POLICY_CLAUSES)} policy clauses, {len(NON_PAYABLE_ITEMS)} non-payable items"
    )


if __name__ == "__main__":
    asyncio.run(main())
