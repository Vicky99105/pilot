"""Contract for digitisation: extract one ClaimRecord from the classified documents.

Every extracted value is a Field with its confidence and the page it came from.
Callers import this module only.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import Code, DocType, Field, PageRef

LOW_CONFIDENCE = 0.85
LOW_CONFIDENCE_HANDWRITTEN = 0.92
SUM_TOLERANCE = Decimal("1.00")  # rupees

# The document types the tracer extracts. Other types are classified but not read.
EXTRACTED_TYPES = (
    DocType.CLAIM_FORM_PART_A,
    DocType.DISCHARGE_SUMMARY,
    DocType.FINAL_BILL,
    DocType.CANCELLED_CHEQUE,
)


def missing() -> Any:
    """A value no document supplied: nothing to show, nothing to trust."""
    return Field(value=None, confidence=0.0)


class Identity(BaseModel):  # from claim form Part A
    patient_name: Field[str] = missing()
    policy_number: Field[str] = missing()
    member_id: Field[str] = missing()
    date_of_birth: Field[date] = missing()
    gender: Field[str] = missing()


class HospitalDetails(BaseModel):  # from claim form Part A
    name: Field[str] = missing()
    city: Field[str] = missing()


class Admission(BaseModel):  # Part A first, then the discharge summary
    admission_date: Field[date] = missing()
    discharge_date: Field[date] = missing()
    accident: Field[bool] = missing()  # Part A: hospitalisation due to an injury or accident


class ClaimedAmounts(BaseModel):  # from claim form Part A
    hospital_expenses: Field[Decimal] = missing()
    pre_hospitalisation: Field[Decimal] = missing()
    post_hospitalisation: Field[Decimal] = missing()
    total: Field[Decimal] = missing()


FactKind = Literal["diagnosis", "procedure", "history", "finding", "medication", "advice"]


class ClinicalFact(BaseModel):
    kind: FactKind
    text: str  # as written
    duration: str | None = None  # duration phrase as written, e.g. "since 5 years"
    confidence: float
    source: PageRef | None = None


class Clinical(BaseModel):  # from the discharge summary
    patient_name: Field[str] = missing()
    diagnoses: list[Field[str]] = []  # as written; coding happens in medical review
    procedures: list[Field[str]] = []
    hospital_codes: list[Code] = []  # codes the hospital wrote, unverified
    facts: list[ClinicalFact] = []


class BillLine(BaseModel):
    line_id: int  # 1-based, in bill order
    head: Field[str]  # section heading as written, e.g. "Room Charges"
    date: Field[date]
    description: Field[str]
    qty: Field[Decimal]
    rate: Field[Decimal]
    amount: Field[Decimal]


class SectionTotal(BaseModel):
    head: str
    total: Field[Decimal]


class Bill(BaseModel):  # from the itemised final bill
    bill_number: Field[str] = missing()
    patient_name: Field[str] = missing()
    lines: list[BillLine] = []
    section_totals: list[SectionTotal] = []
    grand_total: Field[Decimal] = missing()


class Bank(BaseModel):
    account_number: Field[str] = missing()
    ifsc: Field[str] = missing()
    account_holder: Field[str] = missing()


class CheckResult(BaseModel):
    check_id: str
    passed: bool
    detail: str
    fields: list[str] = []  # dotted paths into the ClaimRecord


class ClaimRecord(BaseModel):
    claim_id: UUID
    classification_version: int  # the DocumentSet version this record was read from
    documents: dict[DocType, UUID] = {}  # the document read for each extracted type
    identity: Identity = Identity()
    hospital: HospitalDetails = HospitalDetails()
    admission: Admission = Admission()
    claimed: ClaimedAmounts = ClaimedAmounts()
    clinical: Clinical = Clinical()
    bill: Bill = Bill()
    bank: Bank = Bank()  # from the cancelled cheque
    claim_form_bank: Bank = Bank()  # as typed on claim form Part A
    failed_checks: list[CheckResult] = []
    low_confidence_fields: list[str] = []  # dotted paths
    corrected_fields: list[str] = []  # dotted paths a person has set


class NotClassified(PermanentError):
    """The claim has no classified documents to read."""


class InvalidCorrection(PermanentError):
    """A correction names no field, or its value does not fit the field."""


class DigitisationService(Protocol):
    async def extract(self, claim_id: UUID) -> ClaimRecord:
        """Idempotent per classification version."""
        ...

    async def apply_corrections(
        self, claim_id: UUID, fields: dict[str, Any], *, actor: str
    ) -> ClaimRecord:
        """Values a person set, keyed by dotted path such as "bill.lines.0.amount".
        Checks run again and the record is stored as a new version."""
        ...
