"""Prompts and reply schemas for reading the four tracer document types.

The model sees every page of one document, in order, and returns values as written with a
confidence and the 1-based page each value came from. Code, not the model, does all
arithmetic and comparison.
"""

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator

from app.shared.types import DocType

T = TypeVar("T")


def _amount(raw: Any) -> Any:
    """Accept "Rs. 48,250/-" or 48250.0 as rupees; leave anything else to fail validation."""
    if raw is None or isinstance(raw, Decimal | int):
        return raw
    if isinstance(raw, float):
        return Decimal(str(raw))
    if isinstance(raw, str):
        text = re.sub(r"(?i)rs\.?|inr|₹|/-|,|\s", "", raw)
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return raw
    return raw


Amount = Annotated[Decimal, BeforeValidator(_amount)]


class Extracted(BaseModel, Generic[T]):
    value: T | None = None
    confidence: float = 0.0
    page: int | None = None  # 1-based, among the images sent
    handwritten: bool = False


class PartAReply(BaseModel):
    patient_name: Extracted[str] = Extracted()
    policy_number: Extracted[str] = Extracted()
    member_id: Extracted[str] = Extracted()
    date_of_birth: Extracted[dt.date] = Extracted()
    gender: Extracted[str] = Extracted()
    hospital_name: Extracted[str] = Extracted()
    hospital_city: Extracted[str] = Extracted()
    admission_date: Extracted[dt.date] = Extracted()
    discharge_date: Extracted[dt.date] = Extracted()
    hospitalisation_due_to_injury: Extracted[bool] = Extracted()
    claimed_hospital_expenses: Extracted[Amount] = Extracted()
    claimed_pre_hospitalisation: Extracted[Amount] = Extracted()
    claimed_post_hospitalisation: Extracted[Amount] = Extracted()
    claimed_total: Extracted[Amount] = Extracted()
    bank_account_number: Extracted[str] = Extracted()
    ifsc: Extracted[str] = Extracted()
    account_holder: Extracted[str] = Extracted()


class FactReply(BaseModel):
    kind: Literal["diagnosis", "procedure", "history", "finding", "medication", "advice"]
    text: str
    duration: str | None = None
    page: int | None = None
    confidence: float = 0.0


class DischargeSummaryReply(BaseModel):
    patient_name: Extracted[str] = Extracted()
    admission_date: Extracted[dt.date] = Extracted()
    discharge_date: Extracted[dt.date] = Extracted()
    diagnoses: list[Extracted[str]] = []
    procedures: list[Extracted[str]] = []
    icd_codes_written: list[Extracted[str]] = []
    facts: list[FactReply] = []


class BillLineReply(BaseModel):
    section: str | None = None
    date: dt.date | None = None
    description: str | None = None
    qty: Amount | None = None
    rate: Amount | None = None
    amount: Amount | None = None
    page: int | None = None
    confidence: float = 0.0
    handwritten: bool = False


class SectionTotalReply(BaseModel):
    section: str
    amount: Amount | None = None
    page: int | None = None
    confidence: float = 0.0


class FinalBillReply(BaseModel):
    bill_number: Extracted[str] = Extracted()
    patient_name: Extracted[str] = Extracted()
    lines: list[BillLineReply] = []
    section_totals: list[SectionTotalReply] = []
    grand_total: Extracted[Amount] = Extracted()


class CancelledChequeReply(BaseModel):
    account_number: Extracted[str] = Extracted()
    ifsc: Extracted[str] = Extracted()
    account_holder: Extracted[str] = Extracted()
    bank_name: Extracted[str] = Extracted()


REPLY_SCHEMAS: dict[DocType, type[BaseModel]] = {
    DocType.CLAIM_FORM_PART_A: PartAReply,
    DocType.DISCHARGE_SUMMARY: DischargeSummaryReply,
    DocType.FINAL_BILL: FinalBillReply,
    DocType.CANCELLED_CHEQUE: CancelledChequeReply,
}


def purpose(doc_type: DocType) -> str:
    return f"extract_{doc_type.value}"


_COMMON = (
    "You read one document from an Indian health insurance claim. The images are its "
    "pages, in order; page 1 is the first image.\n"
    "Rules:\n"
    "- Copy values exactly as written. Do not correct, translate or code them.\n"
    "- Dates as YYYY-MM-DD. Amounts in rupees as plain numbers, without commas.\n"
    "- For each value give confidence (0 to 1), the page it is on, and handwritten=true "
    "when the value is handwritten.\n"
    "- When a value is absent, return value null. Use a high confidence only when you are "
    "sure it is absent.\n"
    "- Never calculate a value that is not printed.\n\n"
)

PROMPTS = {
    DocType.CLAIM_FORM_PART_A: _COMMON
    + "Document: claim form Part A, filled by the insured person.\n"
    "Extract: the patient's name, policy number, member or insured ID, date of birth, "
    "gender, the hospital's name and city, the admission and discharge dates, whether the "
    "hospitalisation was due to an injury or accident (true or false, as ticked), the claimed "
    "amounts (hospital expenses, pre-hospitalisation, post-hospitalisation, total), and the "
    "bank account number, IFSC and account holder name.",
    DocType.DISCHARGE_SUMMARY: _COMMON + "Document: hospital discharge summary.\n"
    "Extract: the patient's name; admission and discharge dates; every diagnosis as "
    "written (final and provisional); every procedure or surgery; any ICD codes the "
    "hospital wrote; and clinical facts. A fact is one statement of history, examination "
    "finding, investigation result, medication or discharge advice, each with its page. "
    'For history, put the duration phrase as written in duration, for example "since 5 '
    'years" or "x 2 months".',
    DocType.FINAL_BILL: _COMMON + "Document: itemised hospital final bill.\n"
    "Extract: the bill number, the patient's name, every bill line, every section total "
    "and the grand total. A bill line has the section heading it sits under (as written, "
    'for example "Room Charges" or "Pharmacy"), its date, description, quantity, rate and '
    "amount. Give each line one confidence and its page. List section totals only when "
    "the bill prints them.",
    DocType.CANCELLED_CHEQUE: _COMMON + "Document: cancelled cheque or bank passbook page.\n"
    "Extract: the account number, IFSC code, account holder name and bank name.",
}
