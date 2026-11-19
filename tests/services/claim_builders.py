"""Builders for claims that have reached registration: a claim row, its first submission,
its intake record and an extracted ClaimRecord. Used by the registration and bill review
tests."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.digitisation.contract import (
    Admission,
    Bill,
    BillLine,
    ClaimedAmounts,
    ClaimRecord,
    Clinical,
    ClinicalFact,
    HospitalDetails,
    Identity,
)
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS
from app.shared.types import Code, DocType, Field, PageRef, RequestType, Stage

POLICY = "MOCK/AS/2026/000101"
MEMBER = "MOCK-M-000101-01"
DISCHARGE_DOC = UUID("0d15c4a2-0000-4000-8000-000000000001")
DIAGNOSIS = "Acute appendicitis with perforation and localised peritonitis"


def page(n: int) -> PageRef:
    """A page of the discharge summary."""
    return PageRef(document_id=DISCHARGE_DOC, page=n)


def fact(kind: Any, text: str, n: int, duration: str | None = None) -> ClinicalFact:
    return ClinicalFact(kind=kind, text=text, duration=duration, confidence=0.95, source=page(n))


def appendicitis(*extra: ClinicalFact) -> Clinical:
    """The tracer claim's discharge summary: perforated appendix, hospital code K35.8."""
    return Clinical(
        diagnoses=[Field(value=DIAGNOSIS, confidence=0.95, source=page(1))],
        procedures=[Field(value="Laparoscopic appendicectomy", confidence=0.95, source=page(1))],
        hospital_codes=[Code(system="ICD-10", version="as-written", code="K35.8")],
        facts=[
            fact("diagnosis", DIAGNOSIS, 1),
            fact("history", "Pain in right lower abdomen for 2 days", 1, duration="2 days"),
            fact("finding", "CT abdomen: perforated appendix with localised collection", 2),
            *extra,
        ],
    )


def f(value: Any) -> Any:
    return Field(value=value, confidence=0.99)


def bill_line(line_id: int, head: str, description: str, amount: str, qty: str = "1") -> BillLine:
    amount_d = Decimal(amount)
    qty_d = Decimal(qty)
    return BillLine(
        line_id=line_id,
        head=f(head),
        date=f(date(2026, 11, 2)),
        description=f(description),
        qty=f(qty_d),
        rate=f((amount_d / qty_d).quantize(Decimal("0.01"))),
        amount=f(amount_d),
    )


# The worked example: SI Rs 3 lakh, room Rs 7,500 a day for 4 days, payable Rs 1,33,000.
WORKED_BILL = [
    bill_line(1, "Room Charges", "Deluxe single room", "30000", qty="4"),
    bill_line(2, "Professional Fees", "Surgeon fee", "30000"),
    bill_line(3, "OT Charges", "Operation theatre charges", "20000"),
    bill_line(4, "Nursing Charges", "Nursing care", "10000"),
    bill_line(5, "Pharmacy", "Drugs and medicines", "25000"),
    bill_line(6, "Implants", "Titanium plate and screws", "40000"),
    bill_line(7, "Investigations", "CT scan and blood tests", "15000"),
]


@dataclass
class ClaimSpec:
    patient_name: str = "Asha Verma"
    member_id: str | None = MEMBER
    policy_number: str | None = POLICY
    dob: date | None = date(1984, 6, 12)
    hospital_id: str | None = HOSPITAL_ROHINI_IDS[0]
    hospital_name: str | None = "Sunrise Multispeciality Hospital"
    hospital_city: str | None = "New Delhi"
    admission_date: date = date(2026, 11, 2)
    discharge_date: date = date(2026, 11, 6)
    accident: bool = False
    claimed: Decimal = Decimal("170000.00")
    lines: list[BillLine] = field(default_factory=lambda: list(WORKED_BILL))
    clinical: Clinical = field(default_factory=appendicitis)


def claim_record(claim_id: UUID, spec: ClaimSpec) -> ClaimRecord:
    total = sum((ln.amount.value or Decimal(0) for ln in spec.lines), Decimal(0))
    return ClaimRecord(
        claim_id=claim_id,
        classification_version=1,
        identity=Identity(
            patient_name=f(spec.patient_name),
            policy_number=f(spec.policy_number),
            member_id=f(spec.member_id),
            date_of_birth=f(spec.dob),
        ),
        hospital=HospitalDetails(name=f(spec.hospital_name), city=f(spec.hospital_city)),
        admission=Admission(
            admission_date=f(spec.admission_date),
            discharge_date=f(spec.discharge_date),
            accident=f(spec.accident),
        ),
        claimed=ClaimedAmounts(total=f(spec.claimed)),
        clinical=spec.clinical,
        documents={DocType.DISCHARGE_SUMMARY: DISCHARGE_DOC},
        bill=Bill(lines=spec.lines, grand_total=f(total)),
    )


async def add_digitised_claim(
    sessions: async_sessionmaker[AsyncSession], spec: ClaimSpec | None = None
) -> UUID:
    """A claim with its submission, intake record and one ClaimRecord version."""
    spec = spec or ClaimSpec()
    now = datetime.now(UTC)
    claim_id, submission_id = uuid4(), uuid4()
    form = {
        "policy_number": spec.policy_number or "",
        "member_id": spec.member_id or "",
        "patient_name": spec.patient_name,
        "hospital_id": spec.hospital_id,
        "hospital_name": spec.hospital_name,
        "admission_date": spec.admission_date.isoformat(),
        "discharge_date": spec.discharge_date.isoformat(),
        "claimed_amount": str(spec.claimed),
    }
    async with sessions.begin() as session:
        session.add(
            SubmissionRow(
                id=submission_id,
                request_type=RequestType.REIMBURSEMENT.value,
                form=form,
                member_id=spec.member_id,
                admission_date=spec.admission_date,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            ClaimRow(
                id=claim_id,
                submission_id=submission_id,
                member_id=spec.member_id,
                policy_number=spec.policy_number,
                hospital_id=spec.hospital_id,
                request_type=RequestType.REIMBURSEMENT.value,
                status="digitisation",
                status_changed_at=now,
                created_at=now,
            )
        )
        await session.flush()
        row = await session.get(SubmissionRow, submission_id)
        assert row is not None
        row.claim_id = claim_id
    records = StageRecordRepository(sessions)
    await records.append(claim_id, Stage.INTAKE, {"form": form}, created_by="test")
    await records.append(
        claim_id,
        Stage.DIGITISATION,
        claim_record(claim_id, spec).model_dump(mode="json"),
        created_by="model",
    )
    return claim_id
