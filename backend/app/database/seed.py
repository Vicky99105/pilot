"""Seed the claims database with the Phase 1 users, mock hospitals and document checklists.

Idempotent: rows are merged by primary key (checklists by request type and document type),
so running it twice changes nothing.
Run inside the app container: python -m app.database.seed
"""

import asyncio
from datetime import date
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.models import DocumentChecklistRow, HospitalRow, UserRoleRow, UserRow
from app.shared.seed_ids import (
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    HOSPITAL_ROHINI_IDS,
    HOSPITAL_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    OPS_USER_ID,
)
from app.shared.types import DocType, RequestType, Role

RowT = TypeVar("RowT", UserRow, UserRoleRow, HospitalRow)

SOURCE = "pilot_seed"
SOURCE_VERSION = "2026-11-19"

USERS = [
    UserRow(
        id=MEMBER_USER_ID,
        name="Asha Verma (member)",
        kind="member",
        policy_number=MEMBER_POLICY_NUMBER,
        member_id=MEMBER_MEMBER_ID,
    ),
    UserRow(
        id=HOSPITAL_USER_ID,
        name="Sunrise Hospital desk",
        kind="hospital",
        hospital_id=HOSPITAL_ROHINI_IDS[0],
    ),
    UserRow(id=OPS_USER_ID, name="Ops", kind="staff"),
    UserRow(id=DOCTOR_USER_ID, name="Doctor", kind="staff"),
    UserRow(id=FINANCE_USER_ID, name="Finance", kind="staff"),
]

ROLES = [
    UserRoleRow(user_id=MEMBER_USER_ID, role=Role.MEMBER.value),
    UserRoleRow(user_id=HOSPITAL_USER_ID, role=Role.HOSPITAL_USER.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.CLAIMS_OPERATOR.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.BILL_PROCESSOR.value),
    UserRoleRow(user_id=OPS_USER_ID, role=Role.INVESTIGATOR.value),
    UserRoleRow(user_id=DOCTOR_USER_ID, role=Role.MEDICAL_OFFICER.value),
    UserRoleRow(
        user_id=DOCTOR_USER_ID, role=Role.APPROVER.value, approval_limit=Decimal("100000.00")
    ),
    UserRoleRow(user_id=DOCTOR_USER_ID, role=Role.CRC_REVIEWER.value),
    UserRoleRow(user_id=FINANCE_USER_ID, role=Role.FINANCE.value),
]

_HOSPITALS = [
    ("Sunrise Multispeciality Hospital", "New Delhi", "Delhi", "110017", 120),
    ("Lotus Care Hospital", "Gurugram", "Haryana", "122001", 80),
    ("Ganga Medical Centre", "Lucknow", "Uttar Pradesh", "226010", 150),
    ("Sahyadri Nursing Home", "Pune", "Maharashtra", "411004", 40),
    ("Kaveri General Hospital", "Bengaluru", "Karnataka", "560034", 200),
]

HOSPITALS = [
    HospitalRow(
        rohini_id=rohini,
        name=name,
        city=city,
        state=state,
        pincode=pincode,
        network=True,
        beds=beds,
        registered_on=date(2018, 4, 1),
        source=SOURCE,
        source_version=SOURCE_VERSION,
    )
    for rohini, (name, city, state, pincode, beds) in zip(
        HOSPITAL_ROHINI_IDS, _HOSPITALS, strict=True
    )
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
        await _seed_checklist(session, RequestType.REIMBURSEMENT, REIMBURSEMENT_CHECKLIST)


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
        f"{len(REIMBURSEMENT_CHECKLIST)} reimbursement checklist items"
    )


if __name__ == "__main__":
    asyncio.run(main())
