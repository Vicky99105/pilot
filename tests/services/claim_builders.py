"""Builders for claims that have reached registration: a claim row, its first submission,
its intake record and an extracted ClaimRecord. Used by the registration and bill review
tests."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow, HospitalAgreementRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.bill_review.contract import DeductionSheet, PackageMapping
from app.services.bill_review.service import DefaultBillReviewService
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
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import (
    Citation,
    LookupCall,
    MedicalCoding,
    VerifiedCode,
    VerifiedProcedure,
)
from app.services.rules.contract import MappedPackage
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS
from app.shared.types import Code, DocType, Field, PageRef, RequestType, Stage
from tests.fakes.model_gateway import FakeModelGateway

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
    submitted_by: UUID | None = None
    # How the claim arrived: reimbursement, or a cashless discharge from a network hospital.
    request_type: RequestType = RequestType.REIMBURSEMENT


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
                request_type=spec.request_type.value,
                submitted_by=spec.submitted_by,
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
                request_type=spec.request_type.value,
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


# --- Medical coding (step 1 of medical review), as bill review and the review read it -----

LAP_APPENDICECTOMY = ("Laparoscopic appendicectomy", "0DTJ4ZZ",
                      "Resection of Appendix, Percutaneous Endoscopic Approach")  # fmt: skip
K35_80 = ("K35.80", "Unspecified acute appendicitis")


async def add_coding(
    sessions: async_sessionmaker[AsyncSession],
    claim_id: UUID,
    *,
    procedures: tuple[tuple[str, str, str], ...] = (LAP_APPENDICECTOMY,),
    diagnoses: tuple[tuple[str, str], ...] = (K35_80,),
) -> MedicalCoding:
    """A MedicalCoding record for the claim's latest record versions, with one lookup per
    code so every reference in it holds."""
    records = StageRecordRepository(sessions)
    digitised = await records.latest(claim_id, Stage.DIGITISATION)
    registered = await records.latest(claim_id, Stage.REGISTRATION)
    assert digitised is not None and registered is not None
    lookups, codes, built = [], [], []
    for code, display in diagnoses:
        call = LookupCall(call_id=len(lookups) + 1, tool="search_icd_index", argument=display,
                          returned_codes=[code])  # fmt: skip
        lookups.append(call)
        codes.append(
            VerifiedCode(code=Code(system="ICD-10-CM", version="FY2027", code=code,
                                   display=display),
                         phrase=DIAGNOSIS, lookup_call_id=call.call_id,
                         citations=[Citation(page=page(1))], rationale="test")
        )  # fmt: skip
    for phrase, code, display in procedures:
        call = LookupCall(call_id=len(lookups) + 1, tool="pcs_validate", argument=code,
                          returned_codes=[code])  # fmt: skip
        lookups.append(call)
        built.append(
            VerifiedProcedure(code=Code(system="ICD-10-PCS", version="FY2027", code=code,
                                        display=display),
                              phrase=phrase, steps=[], validate_call_id=call.call_id,
                              citations=[Citation(page=page(1))])
        )  # fmt: skip
    coding = MedicalCoding(
        claim_id=claim_id,
        digitisation_version=digitised.version,
        registration_version=registered.version,
        verified_codes=codes,
        procedure_codes=built,
        lookups=lookups,
        dropped=[],
        revisions=0,
    )
    await records.append(
        claim_id, Stage.MEDICAL_CODING, coding.model_dump(mode="json"), created_by="test"
    )
    return coding


AG095 = ("CGHS", "AG095", "Laparoscopic Appendectomy", "33000")


async def add_sheet(
    sessions: async_sessionmaker[AsyncSession],
    claim_id: UUID,
    *,
    packages: tuple[tuple[str, str, str, str], ...] = (AG095,),
) -> DeductionSheet:
    """A deduction sheet whose package mapping names the packages (scheme, code, name,
    rate), as the full medical review reads it."""
    records = StageRecordRepository(sessions)
    digitised = await records.latest(claim_id, Stage.DIGITISATION)
    registered = await records.latest(claim_id, Stage.REGISTRATION)
    coded = await records.latest(claim_id, Stage.MEDICAL_CODING)
    assert digitised is not None and registered is not None and coded is not None
    zero = Decimal("0.00")
    sheet = DeductionSheet(
        claim_id=claim_id,
        digitisation_version=digitised.version,
        registration_version=registered.version,
        coding_version=coded.version,
        product_uin="PLACEHOLDER-AROGYA-SANJEEVANI-UIN",
        package=PackageMapping(
            candidates=[],
            choices=[],
            packages=[
                MappedPackage(scheme=s, package_code=c, name=n, rate=Decimal(r))
                for s, c, n, r in packages
            ],
            billed_as_package=False,
        )
        if packages
        else None,
        lines=[],
        rule_results=[],
        claimed_total=zero,
        admissible_total=zero,
        co_pay=zero,
        payable=zero,
        flagged_lines=[],
    )
    await records.append(claim_id, Stage.BILL_REVIEW, sheet.model_dump(mode="json"),
                         created_by="test")  # fmt: skip
    return sheet


async def without_agreements(sessions: async_sessionmaker[AsyncSession]) -> None:
    """No hospital has an agreement on file: no package and no non-package discount. Tests of
    later stages use this so the worked example keeps its Rs 1,33,000."""
    async with sessions.begin() as session:
        await session.execute(delete(HospitalAgreementRow))


async def reviewed_bill(sessions: async_sessionmaker[AsyncSession], claim_id: UUID) -> None:
    """Medical coding with no procedure, then a real bill review with no model calls."""
    await add_coding(sessions, claim_id, procedures=())
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    service = DefaultBillReviewService(
        sessions, rules, FakeModelGateway({}), DefaultKnowledgeBase(sessions)
    )
    await service.review(claim_id)
