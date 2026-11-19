"""Types shared by every service. Contracts import these; nothing here has logic."""

from decimal import Decimal
from enum import StrEnum
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel

T = TypeVar("T")


class Code(BaseModel):
    system: str
    version: str
    code: str
    display: str | None = None


class PageRef(BaseModel):
    document_id: UUID
    page: int  # 1-based page within the document


class Field(BaseModel, Generic[T]):
    value: T | None
    confidence: float
    source: PageRef | None = None
    handwritten: bool = False


# How a claim reaches the insurer: through the network hospital (cashless: pre-auth,
# enhancement, discharge) or from the insured person after paying the bill (reimbursement).
ClaimRoute = Literal["cashless", "reimbursement"]


# The system of medicine a treatment belongs to. Medical officers review only their own.
SystemOfMedicine = Literal["allopathy", "ayush"]
DEFAULT_SYSTEM_OF_MEDICINE: SystemOfMedicine = "allopathy"


class RuleResult(BaseModel):
    rule_id: str
    passed: bool
    amount_effect: Decimal = Decimal("0")
    reason: str
    clause_ref: str | None = None
    judged_by: Literal["code", "model", "person"] = "code"


class RequestType(StrEnum):
    PREAUTH = "preauth"
    ENHANCEMENT = "enhancement"
    DISCHARGE = "discharge"
    REIMBURSEMENT = "reimbursement"
    QUERY_REPLY = "query_reply"
    RECONSIDERATION = "reconsideration"
    CANCELLATION = "cancellation"


# The requests a network hospital sends on one cashless claim, one claim per admission. The
# pre-auth opens the claim; the others are follow-ups on it.
CASHLESS_REQUESTS = (
    RequestType.PREAUTH,
    RequestType.ENHANCEMENT,
    RequestType.DISCHARGE,
    RequestType.CANCELLATION,
)


class DocType(StrEnum):
    """Document types a page can be labelled with. Checklists and slots use the same values."""

    CLAIM_FORM_PART_A = "claim_form_part_a"
    CLAIM_FORM_PART_B = "claim_form_part_b"
    FINAL_BILL = "final_bill"
    PAYMENT_RECEIPT = "payment_receipt"
    DISCHARGE_SUMMARY = "discharge_summary"
    OT_NOTES = "ot_notes"
    INVESTIGATION_REPORT = "investigation_report"
    PRESCRIPTION = "prescription"
    PHARMACY_BILL = "pharmacy_bill"
    CANCELLED_CHEQUE = "cancelled_cheque"
    KYC = "kyc"
    FIR_MLC = "fir_mlc"  # police FIR or medico-legal certificate, for an injury or accident
    PREAUTH_FORM = "preauth_form"  # cashless request form, Part C (hospital and doctor)
    ENHANCEMENT_REQUEST = "enhancement_request"  # the hospital's note asking for more
    INTERIM_BILL = "interim_bill"  # the running bill during the stay
    OTHER = "other"


class Stage(StrEnum):
    INTAKE = "intake"
    CLASSIFICATION = "classification"
    DIGITISATION = "digitisation"
    REGISTRATION = "registration"
    MEDICAL_CODING = "medical_coding"  # medical review step 1, before bill review
    BILL_REVIEW = "bill_review"
    MEDICAL_REVIEW = "medical_review"
    RISK = "risk"
    ADJUDICATION = "adjudication"
    DECISION = "decision"
    AUTHORISATION = "authorisation"  # cashless: the approved total and the block behind it
    COMMUNICATION = "communication"
    QUERY = "query"
    SETTLEMENT = "settlement"
    MONITORING = "monitoring"


class Role(StrEnum):
    HOSPITAL_USER = "hospital_user"
    MEMBER = "member"
    CLAIMS_OPERATOR = "claims_operator"
    BILL_PROCESSOR = "bill_processor"
    MEDICAL_OFFICER = "medical_officer"
    APPROVER = "approver"
    CRC_REVIEWER = "crc_reviewer"
    INVESTIGATOR = "investigator"
    FINANCE = "finance"
    QC_AUDITOR = "qc_auditor"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"
