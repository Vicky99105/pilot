"""Prompt and reply schema for labelling one page."""

from pydantic import BaseModel

from app.shared.types import DocType

PURPOSE = "classify_page"
TEXT_LAYER_CHARS = 1500

DOC_TYPE_GUIDE = {
    DocType.CLAIM_FORM_PART_A: (
        "Insurer claim form Part A, filled by the insured: policy and member details, "
        "hospitalisation dates, claimed amounts, bank account"
    ),
    DocType.CLAIM_FORM_PART_B: (
        "Insurer claim form Part B, filled and sealed by the hospital: diagnosis, "
        "treatment, doctor's declaration"
    ),
    DocType.FINAL_BILL: "Hospital final bill, itemised, with section headings and totals",
    DocType.PAYMENT_RECEIPT: "Receipt showing the hospital was paid",
    DocType.DISCHARGE_SUMMARY: (
        "Discharge summary: diagnosis, history, findings, treatment, advice on discharge"
    ),
    DocType.OT_NOTES: (
        "Operation theatre notes: operative record of a surgery, with surgeon, anaesthetist, "
        "anaesthesia, procedure steps and operative findings"
    ),
    DocType.INVESTIGATION_REPORT: "Lab or imaging report (blood test, X-ray, scan)",
    DocType.PRESCRIPTION: "Doctor's prescription",
    DocType.PHARMACY_BILL: "Pharmacy or chemist bill for medicines",
    DocType.CANCELLED_CHEQUE: "Cancelled cheque or bank passbook page showing account and IFSC",
    DocType.KYC: "Identity or address proof: Aadhaar, PAN, passport, voter ID",
    DocType.FIR_MLC: "Police FIR or medico-legal certificate (MLC) for an injury or accident",
    DocType.OTHER: "Anything else",
}


class PageLabelReply(BaseModel):
    doc_type: DocType
    confidence: float  # 0 to 1


def page_prompt(slot: DocType | None, text_layer: str | None) -> str:
    types = "\n".join(f"- {t.value}: {guide}" for t, guide in DOC_TYPE_GUIDE.items())
    hint = slot.value if slot else "none (merged upload)"
    text = (text_layer or "")[:TEXT_LAYER_CHARS] or "(no text layer: a scan or photo)"
    return (
        "You label one page from an Indian health insurance claim submission.\n"
        "Choose the document type this page belongs to:\n"
        f"{types}\n\n"
        f"The uploader filed this page under: {hint}. The uploader can be wrong; trust the "
        "page itself.\n"
        f"Text extracted from the page:\n<<<\n{text}\n>>>\n\n"
        "Reply with doc_type and confidence, a number from 0 to 1 for how sure you are. "
        "Use a confidence below 0.8 when the page is unclear or could be two types."
    )
