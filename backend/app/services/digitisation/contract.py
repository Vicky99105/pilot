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

# The document types digitisation reads. Other types are classified but not read.
EXTRACTED_TYPES = (
    DocType.CLAIM_FORM_PART_A,
    DocType.DISCHARGE_SUMMARY,
    DocType.FINAL_BILL,
    DocType.CANCELLED_CHEQUE,
    DocType.PREAUTH_FORM,
    DocType.ENHANCEMENT_REQUEST,
    DocType.INTERIM_BILL,
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
    # The summary's own dates, kept to cross-check Part A (the admission section's source).
    admission_date: Field[date] = missing()
    discharge_date: Field[date] = missing()
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


class ChequeBank(Bank):  # the cheque also prints the bank's name; Part A has no such field
    bank_name: Field[str] = missing()


class EstimateHead(BaseModel):
    """One head of a cashless cost estimate, as written."""

    head: Field[str]  # e.g. "Room rent", "OT charges"
    days: Field[Decimal] = missing()  # room and ICU heads, when the days are written
    amount: Field[Decimal]


class PreAuthDetails(BaseModel):  # from the pre-auth form, Part C
    rohini_id: Field[str] = missing()
    treating_doctor: Field[str] = missing()
    doctor_registration_number: Field[str] = missing()
    presenting_complaints: Field[str] = missing()
    provisional_diagnosis: Field[str] = missing()
    icd_code_written: Field[str] = missing()
    proposed_treatment: Field[str] = missing()
    pcs_code_written: Field[str] = missing()
    admission_type: Field[str] = missing()  # emergency or planned, as ticked
    expected_stay_days: Field[int] = missing()
    room_type: Field[str] = missing()
    estimate: list[EstimateHead] = []
    estimate_total: Field[Decimal] = missing()


class EnhancementDetails(BaseModel):  # from an enhancement request and its interim bill
    document_id: UUID | None = None  # the enhancement request read
    extra_amount: Field[Decimal] = missing()
    reason: Field[str] = missing()
    extra_days: Field[int] = missing()
    revised_discharge_date: Field[date] = missing()
    estimate: list[EstimateHead] = []  # the extra amount by head
    interim_bill_total: Field[Decimal] = missing()


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
    bank: ChequeBank = ChequeBank()  # from the cancelled cheque
    claim_form_bank: Bank = Bank()  # as typed on claim form Part A
    preauth: PreAuthDetails = PreAuthDetails()  # cashless: Part C
    enhancements: list[EnhancementDetails] = []  # cashless: one per enhancement, in order
    interim_bill: Bill = Bill()  # cashless: the latest interim bill
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
