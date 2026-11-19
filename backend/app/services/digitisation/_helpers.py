"""Assembly of a ClaimRecord from model replies, the consistency checks, the confidence
scan and corrections. Pure functions: no I/O."""

import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from rapidfuzz import fuzz

from app.services.classification.contract import ClassifiedDocument
from app.services.digitisation.contract import (
    LOW_CONFIDENCE,
    LOW_CONFIDENCE_HANDWRITTEN,
    SUM_TOLERANCE,
    Admission,
    Bank,
    Bill,
    BillLine,
    CheckResult,
    ClaimedAmounts,
    ClaimRecord,
    Clinical,
    ClinicalFact,
    HospitalDetails,
    Identity,
    InvalidCorrection,
    SectionTotal,
)
from app.services.digitisation.prompts import (
    BillLineReply,
    CancelledChequeReply,
    DischargeSummaryReply,
    Extracted,
    FinalBillReply,
    PartAReply,
)
from app.services.intake.contract import ClaimForm
from app.shared.types import Code, DocType, Field, PageRef

CENTS = Decimal("0.01")
NAME_MATCH_MIN = 85  # rapidfuzz token_sort_ratio, 0 to 100
_TITLES = {"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "dr", "master", "baby", "kumari"}

# --- Assembly ------------------------------------------------------------------------------


def _clamp(confidence: float) -> float:
    return min(max(confidence, 0.0), 1.0)


def _money(value: Any) -> Any:
    return value.quantize(CENTS) if isinstance(value, Decimal) else value


def _page_ref(doc: ClassifiedDocument, page: int | None) -> PageRef | None:
    if page is not None and 1 <= page <= len(doc.page_ids):
        return PageRef(document_id=doc.document_id, page=page)
    if len(doc.page_ids) == 1:
        return PageRef(document_id=doc.document_id, page=1)
    return None


def _reader(doc: ClassifiedDocument) -> Callable[[Extracted[Any]], Field[Any]]:
    def read(x: Extracted[Any]) -> Field[Any]:
        return Field(
            value=_money(x.value),
            confidence=_clamp(x.confidence),
            source=_page_ref(doc, x.page),
            handwritten=x.handwritten,
        )

    return read


def assemble(
    claim_id: Any,
    classification_version: int,
    docs: dict[DocType, ClassifiedDocument],
    replies: dict[DocType, BaseModel],
) -> ClaimRecord:
    record = ClaimRecord(
        claim_id=claim_id,
        classification_version=classification_version,
        documents={t: d.document_id for t, d in docs.items()},
    )
    part_a = replies.get(DocType.CLAIM_FORM_PART_A)
    if isinstance(part_a, PartAReply):
        _read_part_a(record, part_a, _reader(docs[DocType.CLAIM_FORM_PART_A]))
    summary = replies.get(DocType.DISCHARGE_SUMMARY)
    if isinstance(summary, DischargeSummaryReply):
        _read_discharge_summary(record, summary, docs[DocType.DISCHARGE_SUMMARY])
    bill = replies.get(DocType.FINAL_BILL)
    if isinstance(bill, FinalBillReply):
        record.bill = _read_bill(bill, docs[DocType.FINAL_BILL])
    cheque = replies.get(DocType.CANCELLED_CHEQUE)
    if isinstance(cheque, CancelledChequeReply):
        read = _reader(docs[DocType.CANCELLED_CHEQUE])
        record.bank = Bank(
            account_number=read(cheque.account_number),
            ifsc=read(_upper(cheque.ifsc)),
            account_holder=read(cheque.account_holder),
        )
    return record


def _upper(x: Extracted[str]) -> Extracted[str]:
    return x.model_copy(update={"value": x.value.strip().upper() if x.value else x.value})


def _read_part_a(
    record: ClaimRecord, r: PartAReply, read: Callable[[Extracted[Any]], Field[Any]]
) -> None:
    record.identity = Identity(
        patient_name=read(r.patient_name),
        policy_number=read(r.policy_number),
        member_id=read(r.member_id),
        date_of_birth=read(r.date_of_birth),
        gender=read(r.gender),
    )
    record.hospital = HospitalDetails(name=read(r.hospital_name), city=read(r.hospital_city))
    record.admission = Admission(
        admission_date=read(r.admission_date), discharge_date=read(r.discharge_date)
    )
    record.claimed = ClaimedAmounts(
        hospital_expenses=read(r.claimed_hospital_expenses),
        pre_hospitalisation=read(r.claimed_pre_hospitalisation),
        post_hospitalisation=read(r.claimed_post_hospitalisation),
        total=read(r.claimed_total),
    )
    record.claim_form_bank = Bank(
        account_number=read(r.bank_account_number),
        ifsc=read(_upper(r.ifsc)),
        account_holder=read(r.account_holder),
    )


def _read_discharge_summary(
    record: ClaimRecord, r: DischargeSummaryReply, doc: ClassifiedDocument
) -> None:
    read = _reader(doc)
    record.clinical = Clinical(
        patient_name=read(r.patient_name),
        diagnoses=[read(d) for d in r.diagnoses],
        procedures=[read(p) for p in r.procedures],
        hospital_codes=[
            Code(system="ICD-10", version="as-written", code=c.value.strip())
            for c in r.icd_codes_written
            if c.value
        ],
        facts=[
            ClinicalFact(
                kind=f.kind,
                text=f.text,
                duration=f.duration,
                confidence=_clamp(f.confidence),
                source=_page_ref(doc, f.page),
            )
            for f in r.facts
        ],
    )
    # Part A is the primary source for dates; the summary fills a gap.
    if record.admission.admission_date.value is None:
        record.admission.admission_date = read(r.admission_date)
    if record.admission.discharge_date.value is None:
        record.admission.discharge_date = read(r.discharge_date)


def _read_bill(r: FinalBillReply, doc: ClassifiedDocument) -> Bill:
    read = _reader(doc)

    def cell(line: BillLineReply, value: Any) -> Field[Any]:
        return Field(
            value=_money(value),
            confidence=_clamp(line.confidence),
            source=_page_ref(doc, line.page),
            handwritten=line.handwritten,
        )

    return Bill(
        bill_number=read(r.bill_number),
        patient_name=read(r.patient_name),
        lines=[
            BillLine(
                line_id=i,
                head=cell(line, line.section),
                date=cell(line, line.date),
                description=cell(line, line.description),
                qty=cell(line, line.qty),
                rate=cell(line, line.rate),
                amount=cell(line, line.amount),
            )
            for i, line in enumerate(r.lines, start=1)
        ],
        section_totals=[
            SectionTotal(
                head=s.section,
                total=Field(
                    value=_money(s.amount),
                    confidence=_clamp(s.confidence),
                    source=_page_ref(doc, s.page),
                ),
            )
            for s in r.section_totals
        ],
        grand_total=read(r.grand_total),
    )


# --- Consistency checks -------------------------------------------------------------------


def failed_checks(record: ClaimRecord, form: ClaimForm | None) -> list[CheckResult]:
    results = [
        _section_totals(record.bill),
        _grand_total(record.bill),
        _dates_in_order(record.admission),
        _names_match(record),
        _form_matches(record, form) if form else None,
        _bank_matches(record),
    ]
    return [r for r in results if r is not None and not r.passed]


def _head(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _section_totals(bill: Bill) -> CheckResult | None:
    if not bill.section_totals:
        return None
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for line in bill.lines:
        if line.amount.value is not None:
            sums[_head(line.head.value)] += line.amount.value
    problems, fields = [], []
    for i, section in enumerate(bill.section_totals):
        total = section.total.value
        lines_sum = sums.get(_head(section.head), Decimal("0"))
        if total is not None and abs(lines_sum - total) > SUM_TOLERANCE:
            problems.append(f"{section.head}: lines sum to {lines_sum:.2f}, total {total:.2f}")
            fields.append(f"bill.section_totals.{i}.total")
    return CheckResult(
        check_id="bill_section_totals",
        passed=not problems,
        detail="; ".join(problems) or "section totals match their lines",
        fields=fields,
    )


def _grand_total(bill: Bill) -> CheckResult | None:
    total = bill.grand_total.value
    amounts = [line.amount.value for line in bill.lines if line.amount.value is not None]
    if total is None or not amounts:
        return None
    lines_sum = sum(amounts, Decimal("0"))
    passed = abs(lines_sum - total) <= SUM_TOLERANCE
    return CheckResult(
        check_id="bill_grand_total",
        passed=passed,
        detail=f"lines sum to {lines_sum:.2f}, grand total {total:.2f}",
        fields=[] if passed else ["bill.grand_total"],
    )


def _dates_in_order(admission: Admission) -> CheckResult | None:
    start, end = admission.admission_date.value, admission.discharge_date.value
    if start is None or end is None:
        return None
    passed = start <= end
    return CheckResult(
        check_id="dates_in_order",
        passed=passed,
        detail=f"admitted {start}, discharged {end}",
        fields=[] if passed else ["admission.admission_date", "admission.discharge_date"],
    )


def normalise_name(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in _TITLES)


def names_match(a: str, b: str) -> bool:
    return fuzz.token_sort_ratio(normalise_name(a), normalise_name(b)) >= NAME_MATCH_MIN


def _names_match(record: ClaimRecord) -> CheckResult | None:
    seen = {
        path: name
        for path, name in (
            ("identity.patient_name", record.identity.patient_name.value),
            ("clinical.patient_name", record.clinical.patient_name.value),
            ("bill.patient_name", record.bill.patient_name.value),
        )
        if name
    }
    if len(seen) < 2:
        return None
    # The reference is the name most others agree with; ties go to the claim form.
    reference = max(seen, key=lambda p: sum(names_match(seen[p], n) for n in seen.values()))
    odd = [p for p, n in seen.items() if not names_match(seen[reference], n)]
    return CheckResult(
        check_id="patient_name_matches",
        passed=not odd,
        detail=", ".join(f"{p}: {seen[p]!r}" for p in seen),
        fields=odd,
    )


def _ident(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _form_matches(record: ClaimRecord, form: ClaimForm) -> CheckResult | None:
    problems: list[tuple[str, str]] = []
    policy = record.identity.policy_number.value
    if policy and _ident(policy) != _ident(form.policy_number):
        problems.append(("identity.policy_number", f"policy {form.policy_number} vs {policy}"))
    name = record.identity.patient_name.value
    if name and not names_match(name, form.patient_name):
        problems.append(("identity.patient_name", f"patient {form.patient_name} vs {name}"))
    admitted = record.admission.admission_date.value
    if admitted and admitted != form.admission_date:
        problems.append(
            ("admission.admission_date", f"admitted {form.admission_date} vs {admitted}")
        )
    claimed = record.claimed.total.value
    if claimed is not None and abs(claimed - form.claimed_amount) > SUM_TOLERANCE:
        problems.append(("claimed.total", f"claimed {form.claimed_amount} vs {claimed}"))
    return CheckResult(
        check_id="form_matches_documents",
        passed=not problems,
        detail="; ".join(d for _, d in problems) or "form matches the documents",
        fields=[p for p, _ in problems],
    )


def _bank_matches(record: ClaimRecord) -> CheckResult | None:
    cheque, form = record.bank, record.claim_form_bank
    pairs = [
        ("bank.account_number", cheque.account_number.value, form.account_number.value),
        ("bank.ifsc", cheque.ifsc.value, form.ifsc.value),
    ]
    compared = [(p, a, b) for p, a, b in pairs if a and b]
    if not compared:
        return None
    odd = [p for p, a, b in compared if _ident(a) != _ident(b)]
    return CheckResult(
        check_id="bank_matches_cheque",
        passed=not odd,
        detail="; ".join(f"{p}: cheque {a} vs claim form {b}" for p, a, b in compared),
        fields=odd,
    )


# --- Confidence scan and corrections ------------------------------------------------------


def _fields(value: Any, path: str) -> Iterator[tuple[str, Field[Any]]]:
    if isinstance(value, Field):
        yield path, value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _fields(getattr(value, name), f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _fields(item, f"{path}.{i}")


def low_confidence_fields(record: ClaimRecord) -> list[str]:
    return [
        path
        for path, field in _fields(record, "")
        if field.confidence < (LOW_CONFIDENCE_HANDWRITTEN if field.handwritten else LOW_CONFIDENCE)
    ]


def quantize_money(record: ClaimRecord) -> None:
    for _, field in _fields(record, ""):
        field.value = _money(field.value)


def set_field(data: dict[str, Any], path: str, value: Any) -> None:
    """Set the Field at a dotted path in a dumped ClaimRecord, as a person's value."""
    node: Any = data
    for part in path.split("."):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise InvalidCorrection(f"no field at {path}")
    if not (isinstance(node, dict) and {"value", "confidence"} <= node.keys()):
        raise InvalidCorrection(f"{path} is not a single field")
    node.update(value=value, confidence=1.0, handwritten=False)
