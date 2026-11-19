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
    ChequeBank,
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
        record.bank = ChequeBank(
            account_number=read(cheque.account_number),
            ifsc=read(_upper(cheque.ifsc)),
            account_holder=read(cheque.account_holder),
            bank_name=read(cheque.bank_name),
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
        admission_date=read(r.admission_date),
        discharge_date=read(r.discharge_date),
        accident=read(r.hospitalisation_due_to_injury),
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
        admission_date=read(r.admission_date),
        discharge_date=read(r.discharge_date),
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


def failed_checks(
    record: ClaimRecord, form: ClaimForm | None, held: frozenset[DocType] = frozenset()
) -> list[CheckResult]:
    """held: the document types in any of the claim's document sets."""
    results = [
        _section_totals(record.bill),
        _grand_total(record.bill),
        _dates_in_order(record.admission),
        _names_match(record),
        _form_matches(record, form) if form else None,
        _bank_matches(record),
        _accident_evidence(record, held),
        _dates_agree(record),
        _ifsc_format(record),
        _ifsc_bank_code(record),
        _claimed_matches_bill(record),
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
        ("account_number", cheque.account_number.value, form.account_number.value),
        ("ifsc", cheque.ifsc.value, form.ifsc.value),
    ]
    compared = [(name, a, b) for name, a, b in pairs if a and b]
    if not compared:
        return None
    # Either side can be the misread one, so a mismatch flags both for the operator.
    odd = [
        path
        for name, a, b in compared
        if _ident(a) != _ident(b)
        for path in (f"bank.{name}", f"claim_form_bank.{name}")
    ]
    return CheckResult(
        check_id="bank_matches_cheque",
        passed=not odd,
        detail="; ".join(f"bank.{n}: cheque {a} vs claim form {b}" for n, a, b in compared),
        fields=odd,
    )


# --- Cross-checks against independent evidence ---------------------------------------------
# The model can be wrong at confidence 1.0, so a high-impact field is also compared with
# evidence from another document.

# Words that mean an injury or accident. "wound" is left out: surgical wounds are routine.
_INJURY = re.compile(
    r"\b(injur(?:y|ies|ed)|trauma(?:tic)?|accident(?:al)?|fractur(?:e|es|ed)|rta|"
    r"road traffic|assault(?:ed)?|fell|fall from|burns|laceration|contusion|dislocation|"
    r"(?:dog|snake|animal) bite)\b",
    re.IGNORECASE,
)
# A negation earlier in the same clause: "No history of trauma".
_NEGATED = re.compile(r"\b(no|not|nil|denies|denied|without|negative)\b", re.IGNORECASE)
_CLAUSE_START = re.compile(r"[.;:\n]")


def _injury_mentions(record: ClaimRecord) -> list[str]:
    """Discharge summary diagnoses and facts that state an injury, quoted with their page."""
    texts = [(d.value, d.source) for d in record.clinical.diagnoses if d.value]
    texts += [(f.text, f.source) for f in record.clinical.facts]
    found = []
    for text, source in texts:
        for match in _INJURY.finditer(text):
            clause = _CLAUSE_START.split(text[: match.start()])[-1]
            if not _NEGATED.search(clause):
                page = f" (page {source.page})" if source else ""
                found.append(f"{text!r}{page}")
                break
    return found


def _accident_evidence(record: ClaimRecord, held: frozenset[DocType]) -> CheckResult | None:
    """Part A's injury or accident tick against an FIR or MLC document and the discharge
    summary's injury facts."""
    accident = record.admission.accident.value
    if accident is None:
        return None
    fir_mlc = DocType.FIR_MLC in held
    injuries = _injury_mentions(record) if DocType.DISCHARGE_SUMMARY in record.documents else []
    if accident:
        passed = fir_mlc or bool(injuries)
        evidence = "no FIR or MLC document and no injury in the discharge summary"
    else:
        passed = not fir_mlc and not injuries
        evidence = "; ".join(
            [*([f"a {DocType.FIR_MLC.value} document is attached"] if fir_mlc else []), *injuries]
        )
    return CheckResult(
        check_id="accident_evidence",
        passed=passed,
        detail=f"Part A injury or accident: {'yes' if accident else 'no'}; "
        + (evidence if not passed else "the documents agree"),
        fields=[] if passed else ["admission.accident"],
    )


def _dates_agree(record: ClaimRecord) -> CheckResult | None:
    """Admission and discharge dates on Part A against the discharge summary, and every bill
    line date within the stay."""
    admission, clinical = record.admission, record.clinical
    problems: list[tuple[list[str], str]] = []
    for name, part_a, summary in (
        ("admission_date", admission.admission_date.value, clinical.admission_date.value),
        ("discharge_date", admission.discharge_date.value, clinical.discharge_date.value),
    ):
        if part_a and summary and part_a != summary:
            problems.append(
                (
                    [f"admission.{name}", f"clinical.{name}"],
                    f"{name}: Part A {part_a} vs discharge summary {summary}",
                )
            )
    start, end = admission.admission_date.value, admission.discharge_date.value
    if start and end:
        for i, line in enumerate(record.bill.lines):
            day = line.date.value
            if day and not start <= day <= end:
                problems.append(
                    (
                        [f"bill.lines.{i}.date"],
                        f"bill line {line.line_id} dated {day}, stay {start} to {end}",
                    )
                )
    if not problems and not (start and end):
        return None
    return CheckResult(
        check_id="dates_agree",
        passed=not problems,
        detail="; ".join(d for _, d in problems) or "dates agree across the documents",
        fields=[p for paths, _ in problems for p in paths],
    )


IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_IFSC_PATHS = ("bank.ifsc", "claim_form_bank.ifsc")


def _ifscs(record: ClaimRecord) -> list[tuple[str, str]]:
    values = (record.bank.ifsc.value, record.claim_form_bank.ifsc.value)
    return [(p, v.strip().upper()) for p, v in zip(_IFSC_PATHS, values, strict=True) if v]


def _ifsc_format(record: ClaimRecord) -> CheckResult | None:
    ifscs = _ifscs(record)
    if not ifscs:
        return None
    bad = [(p, v) for p, v in ifscs if not IFSC_PATTERN.match(v)]
    return CheckResult(
        check_id="ifsc_format",
        passed=not bad,
        detail="; ".join(f"{p} {v} is not 4 letters, 0, then 6 characters" for p, v in bad)
        or "every IFSC has the IFSC format",
        fields=[p for p, _ in bad],
    )


# IFSC bank codes (the first 4 characters) of common banks, with the names a cheque prints.
# A code not listed here is not judged.
BANK_CODES: dict[str, tuple[str, ...]] = {
    "SBIN": ("state bank of india", "sbi"),
    "HDFC": ("hdfc bank",),
    "ICIC": ("icici bank",),
    "UTIB": ("axis bank",),
    "PUNB": ("punjab national bank", "pnb"),
    "BARB": ("bank of baroda",),
    "CNRB": ("canara bank",),
    "UBIN": ("union bank of india",),
    "KKBK": ("kotak mahindra bank",),
    "IDIB": ("indian bank",),
    "BKID": ("bank of india",),
    "YESB": ("yes bank",),
    "INDB": ("indusind bank",),
    "IOBA": ("indian overseas bank",),
    "MAHB": ("bank of maharashtra",),
    "CBIN": ("central bank of india",),
    "UCBA": ("uco bank",),
    "PSIB": ("punjab and sind bank",),
    "FDRL": ("federal bank",),
    "IDFB": ("idfc first bank",),
}


def _bank_words(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in {"the", "ltd", "limited"})


def _ifsc_bank_code(record: ClaimRecord) -> CheckResult | None:
    """The bank code of each IFSC against the bank name printed on the cheque."""
    name = record.bank.bank_name.value
    if not name:
        return None
    words = _bank_words(name)
    judged = [(p, v) for p, v in _ifscs(record) if v[:4] in BANK_CODES]
    if not judged:
        return None
    odd = [
        (p, v)
        for p, v in judged
        if not any(words == a or words.startswith(a + " ") for a in BANK_CODES[v[:4]])
    ]
    return CheckResult(
        check_id="ifsc_bank_code",
        passed=not odd,
        detail="; ".join(
            f"{p} {v}: bank code {v[:4]} is {BANK_CODES[v[:4]][0].title()}, "
            f"the cheque names {name!r}"
            for p, v in odd
        )
        or "the IFSC bank codes match the cheque",
        fields=[*(p for p, _ in odd), "bank.bank_name"] if odd else [],
    )


def _claimed_matches_bill(record: ClaimRecord) -> CheckResult | None:
    """The hospital expenses claimed on Part A against the final bill's grand total."""
    claimed = record.claimed
    path, value = "claimed.hospital_expenses", claimed.hospital_expenses.value
    if value is None:
        path, value = "claimed.total", claimed.total.value
    total = record.bill.grand_total.value
    if value is None or total is None:
        return None
    passed = abs(value - total) <= SUM_TOLERANCE
    return CheckResult(
        check_id="claimed_matches_bill",
        passed=passed,
        detail=f"{path} {value:.2f} vs bill grand total {total:.2f}",
        fields=[] if passed else [path, "bill.grand_total"],
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
