"""Generate the tracer acceptance bundle: one member reimbursement claim as PDFs, plus the
ground truth a live run is scored against.

Run from the repo root:

    uv run --project backend python tools/mockgen/tracer_claim.py --out <dir>
    uv run --project backend python tools/mockgen/tracer_claim.py --out <dir> \
        --admission-date 2026-10-29

The claim:
- Member: Asha Verma, the seeded member user's own policy (MOCK/AS/2026/000101). The policy
  began more than three years before the seed day, so R5 (the 30-day initial wait) passes.
  Sum insured Rs 5 lakh, so the room cap is min(2% of Rs 5 lakh, Rs 5,000) = Rs 5,000 a day.
- Hospital: Sunrise Multispeciality Hospital, New Delhi, a seeded network hospital with
  differential billing, so a room breach also reduces nursing, OT and professional fees.
- Treatment: acute appendicitis, laparoscopic appendicectomy, 4 days in a deluxe room at
  Rs 7,500 a day. R13 deducts the room excess and the proportionate share of the linked heads.
  One consumable line, "Gloves", is on the seeded non-payable list I, so R11 deducts it.
- Bundle: claim form Part A (typed), claim form Part B (hospital), discharge summary,
  itemised final bill, cancelled cheque. The payment receipt is left out on purpose, so a
  query is raised; it is written separately as the query reply.

The ground truth's deductions come from the real rules engine (R11, R13, R21) run on the
ground-truth bill lines, so every expected amount is exact. All data is synthetic.
"""

import argparse
import io
import json
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "backend") not in sys.path:  # run as a script: make the app package importable
    sys.path.insert(0, str(REPO / "backend"))

import pypdfium2 as pdfium  # noqa: E402
from PIL import Image, ImageChops, ImageDraw  # noqa: E402
from reportlab.lib.colors import Color, black, white  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen.canvas import Canvas  # noqa: E402

from app.database.seed_knowledge import NON_PAYABLE_ITEMS  # noqa: E402
from app.services.rules.contract import (  # noqa: E402
    Facts,
    Head,
    LineFacts,
    NonPayableItem,
    RoomFacts,
    RulesEngine,
)
from app.services.rules.service import DefaultRulesEngine  # noqa: E402
from app.shared.seed_ids import (  # noqa: E402
    HOSPITAL_ROHINI_IDS,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    PRODUCT_UIN,
)
from app.shared.types import DocType  # noqa: E402

CENTS = Decimal("0.01")
D = Decimal

# --- The member, hospital and bank (synthetic; must match the seeds) ------------------------

# Policy 1 in backend/app/mock_core/seed.py. tests/tools checks these against the seed.
PATIENT = "Asha Verma"
PATIENT_DOB = date(1984, 6, 12)
PATIENT_GENDER = "Female"
SUM_INSURED = D("500000")
MEMBER_EMAIL = "asha.verma@member.example"

# The first seeded hospital in backend/app/database/seed.py (differential billing).
HOSPITAL = "Sunrise Multispeciality Hospital"
HOSPITAL_CITY = "New Delhi"
HOSPITAL_ROHINI = HOSPITAL_ROHINI_IDS[0]
HOSPITAL_ADDRESS = "Plot 14, Press Enclave Road, Saket, New Delhi 110017"
DIFFERENTIAL_BILLING = True

# A fictitious bank. The IFSC has the real format: 4 letters, 0, then 6 characters.
BANK = "Bharat Sahakari Bank Ltd."
BANK_BRANCH = "Saket, New Delhi"
ACCOUNT_NUMBER = "30214567891"
IFSC = "BSBL0004217"
ACCOUNT_HOLDER = "ASHA VERMA"

SURGEON = "Dr. Rajiv Khanna"
ANAESTHETIST = "Dr. Meera Joshi"
DIAGNOSIS = "Acute appendicitis"
PROCEDURE = "Laparoscopic appendicectomy"
HOSPITAL_ICD = "K35.8"  # less specific than the code medical review should verify
HISTORY_DURATION = "since 2 days"
STAY_DAYS = 4
ROOM_RATE = D("7500.00")
NON_PAYABLE_ITEM = "Gloves"  # seeded non-payable list I, item 11


# --- Bill lines -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    section: str  # the bill's section heading, as printed
    head: Head  # the standard head bill review should map the line to
    day: int  # days after admission
    description: str
    qty: Decimal
    rate: Decimal

    @property
    def amount(self) -> Decimal:
        return money(self.qty * self.rate)


ROOM, NURSING, OT = "ROOM CHARGES", "NURSING CHARGES", "OT CHARGES"
FEES, PHARMACY = "PROFESSIONAL FEES", "PHARMACY"
CONSUMABLES, INVESTIGATIONS = "CONSUMABLES", "INVESTIGATIONS"


def _items() -> list[Item]:
    one = D("1")
    items = [Item(ROOM, Head.ROOM, d, "Deluxe Single Room Rent", one, ROOM_RATE) for d in range(4)]
    items += [
        Item(NURSING, Head.NURSING, d, "Nursing Care Charges", one, D("900.00")) for d in range(4)
    ]
    items += [
        Item(OT, Head.OT, 1, "Operation Theatre Charges - Major (up to 2 hrs)", one, D("14500")),
        Item(OT, Head.OT, 1, "Laparoscopy Equipment Charges", one, D("4000.00")),
        Item(
            FEES,
            Head.PROFESSIONAL_FEES,
            1,
            f"Surgeon Fee - {SURGEON} (Laparoscopic Appendicectomy)",
            one,
            D("18000.00"),
        ),
        Item(FEES, Head.PROFESSIONAL_FEES, 1, f"Anaesthetist Fee - {ANAESTHETIST}", one, D("6600")),
    ]
    items += [
        Item(FEES, Head.PROFESSIONAL_FEES, d, f"Consultant Visit - {SURGEON}", one, D("1200.00"))
        for d in (0, 2, 3)
    ]
    items += [
        Item(PHARMACY, Head.PHARMACY, 0, "Inj. Ceftriaxone 1 g", D("6"), D("68.50")),
        Item(PHARMACY, Head.PHARMACY, 0, "Inj. Metronidazole 100 ml", D("6"), D("32.00")),
        Item(PHARMACY, Head.PHARMACY, 0, "Inj. Pantoprazole 40 mg", D("5"), D("54.00")),
        Item(PHARMACY, Head.PHARMACY, 0, "IV Fluid Ringer Lactate 500 ml", D("8"), D("46.00")),
        Item(PHARMACY, Head.PHARMACY, 1, "Inj. Tramadol 50 mg", D("4"), D("18.50")),
        Item(PHARMACY, Head.PHARMACY, 4, "Tab. Cefuroxime 500 mg (discharge)", D("10"), D("42.80")),
        Item(CONSUMABLES, Head.CONSUMABLES, 0, "IV Cannula 20G", D("2"), D("95.00")),
        Item(
            CONSUMABLES,
            Head.CONSUMABLES,
            1,
            "Laparoscopic Trocar Kit (disposable)",
            one,
            D("4850.00"),
        ),
        Item(CONSUMABLES, Head.CONSUMABLES, 1, "Absorbable Suture Vicryl 2-0", D("2"), D("610")),
        Item(CONSUMABLES, Head.CONSUMABLES, 1, NON_PAYABLE_ITEM, D("12"), D("22.00")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "Complete Blood Count (CBC)", one, D("420")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "C-Reactive Protein (CRP)", one, D("650")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "Ultrasound Abdomen and Pelvis", one, D("1900")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "Serum Creatinine", one, D("280.00")),
        Item(
            INVESTIGATIONS,
            Head.DIAGNOSTICS,
            2,
            "Histopathology - Appendix Specimen",
            one,
            D("1450.00"),
        ),
    ]
    return items


SECTION_ORDER = [ROOM, NURSING, OT, FEES, PHARMACY, CONSUMABLES, INVESTIGATIONS]
PAGE_ONE_SECTIONS = {ROOM, NURSING, OT, FEES}  # the rest print on page 2 of the bill


@dataclass(frozen=True)
class TracerClaim:
    admission: date
    items: list[Item] = field(default_factory=_items)

    @property
    def discharge(self) -> date:
        return self.admission + timedelta(days=STAY_DAYS)

    @property
    def surgery(self) -> date:
        return self.admission + timedelta(days=1)

    @property
    def serial(self) -> str:
        """A per-admission serial, so every run's documents (and file hashes) differ."""
        return f"{self.admission.toordinal() % 100000:05d}"

    @property
    def bill_number(self) -> str:
        return f"SMH/IP/{self.admission:%y%m}/{self.serial}"

    @property
    def ip_number(self) -> str:
        return f"IP{self.admission:%y}-{self.serial}"

    @property
    def uhid(self) -> str:
        return "SMH-UH-0418827"

    @property
    def receipt_number(self) -> str:
        return f"SMH/RCPT/{self.discharge:%y%m}/{self.serial}"

    @property
    def age(self) -> int:
        a, b = self.admission, PATIENT_DOB
        return a.year - b.year - ((a.month, a.day) < (b.month, b.day))

    def on(self, day: int) -> date:
        return self.admission + timedelta(days=day)

    def section_items(self, section: str) -> list[Item]:
        return [i for i in self.items if i.section == section]

    def section_total(self, section: str) -> Decimal:
        return money(sum((i.amount for i in self.section_items(section)), D("0")))

    @property
    def grand_total(self) -> Decimal:
        return money(sum((i.amount for i in self.items), D("0")))

    @property
    def ordered_items(self) -> list[Item]:
        """Bill order: by section, then by date within the section."""
        return [
            i for s in SECTION_ORDER for i in sorted(self.section_items(s), key=lambda x: x.day)
        ]


# --- Money and text helpers ------------------------------------------------------------------


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def inr(value: Decimal) -> str:
    """Indian digit grouping: 1,23,456.00."""
    whole, frac = f"{money(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{whole}.{frac}"


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + _words(rest))
    for size, name in ((10**7, "crore"), (10**5, "lakh"), (1000, "thousand")):
        if n >= size:
            rest = n % size
            return _words(n // size) + f" {name}" + ("" if rest == 0 else " " + _words(rest))
    raise AssertionError(n)


def rupees_in_words(value: Decimal) -> str:
    whole = int(value)
    paise = int((money(value) - whole) * 100)
    text = f"Rupees {_words(whole).title()}"
    if paise:
        text += f" and {_words(paise).title()} Paise"
    return text + " Only"


def dmy(day: date) -> str:
    return f"{day:%d/%m/%Y}"


def dmon(day: date) -> str:
    return f"{day:%d-%b-%Y}"


# --- Ground truth ----------------------------------------------------------------------------


def _non_payable() -> list[NonPayableItem]:
    return [
        NonPayableItem(list_name=list_name, item_number=number, item=item, condition=condition)
        for list_name, number, item, _, condition in NON_PAYABLE_ITEMS
    ]


def expected_sheet(claim: TracerClaim, rules: RulesEngine) -> dict[str, Any]:
    """Bill rules on the ground-truth lines through the real rules engine, in bill review's
    order: R11 per line, R13 per line still payable (room rate from those lines), R21 last."""
    product = rules.product(PRODUCT_UIN)
    non_payable = _non_payable()
    lines: list[dict[str, Any]] = []
    facts: dict[int, LineFacts] = {}
    for line_id, item in enumerate(claim.ordered_items, start=1):
        facts[line_id] = LineFacts(
            line_id=line_id,
            head=item.head,
            description=item.description,
            qty=item.qty,
            rate=item.rate,
            claimed=item.amount,
        )
        [r11] = rules.evaluate(
            ["R11"], Facts(line=facts[line_id], non_payable=non_payable), product
        )
        line: dict[str, Any] = {
            "line_id": line_id,
            "head": item.head.value,
            "description": item.description,
            "claimed": item.amount,
            "admissible": item.amount,
            "deducted": D("0.00"),
            "rule_id": None,
            "clause_ref": None,
            "reason": None,
        }
        if not r11.passed:
            _deduct(line, r11.rule_id, r11.clause_ref, r11.amount_effect, r11.reason)
        lines.append(line)
    payable = [ln for ln in lines if ln["rule_id"] is None]
    room = [ln for ln in payable if ln["head"] == Head.ROOM.value]
    room_days = sum((facts[ln["line_id"]].qty or D("1") for ln in room), D("0"))
    room_rate = sum((ln["claimed"] for ln in room), D("0")) / room_days
    room_facts = RoomFacts(
        sum_insured=SUM_INSURED,
        room_rate_per_day=room_rate,
        differential_billing=DIFFERENTIAL_BILLING,
    )
    for line in payable:
        [r13] = rules.evaluate(
            ["R13"], Facts(line=facts[line["line_id"]], room=room_facts), product
        )
        if r13.amount_effect != 0:
            _deduct(line, r13.rule_id, r13.clause_ref, r13.amount_effect, r13.reason)
    admissible = money(sum((ln["admissible"] for ln in lines), D("0")))
    [r21] = rules.evaluate(["R21"], Facts(admissible_total=admissible), product)
    co_pay = money(-r21.amount_effect)
    cap = min(
        SUM_INSURED * product.room_rent.percent_of_sum_insured / 100, product.room_rent.max_per_day
    )
    return {
        "room_rate_per_day": money(room_rate),
        "room_cap_per_day": money(cap),
        "lines": lines,
        "deductions": [ln for ln in lines if ln["deducted"] > 0],
        "claimed_total": claim.grand_total,
        "admissible_total": admissible,
        "co_pay": co_pay,
        "co_pay_rule": {"rule_id": r21.rule_id, "clause_ref": r21.clause_ref, "reason": r21.reason},
        "payable": money(admissible - co_pay),
    }


def _deduct(
    line: dict[str, Any], rule_id: str, clause: str | None, effect: Decimal, reason: str
) -> None:
    line["admissible"] = money(max(line["claimed"] + effect, D("0")))
    line["deducted"] = money(line["claimed"] - line["admissible"])
    line["rule_id"], line["clause_ref"], line["reason"] = rule_id, clause, reason


def ground_truth(claim: TracerClaim, rules: RulesEngine, files: dict[str, Any]) -> dict[str, Any]:
    total = claim.grand_total
    sheet = expected_sheet(claim, rules)
    fields: dict[str, Any] = {
        "identity.patient_name": PATIENT,
        "identity.policy_number": MEMBER_POLICY_NUMBER,
        "identity.member_id": MEMBER_MEMBER_ID,
        "identity.date_of_birth": PATIENT_DOB,
        "identity.gender": PATIENT_GENDER,
        "hospital.name": HOSPITAL,
        "hospital.city": HOSPITAL_CITY,
        "admission.admission_date": claim.admission,
        "admission.discharge_date": claim.discharge,
        "admission.accident": False,
        "claimed.hospital_expenses": total,
        "claimed.pre_hospitalisation": D("0.00"),
        "claimed.post_hospitalisation": D("0.00"),
        "claimed.total": total,
        "claim_form_bank.account_number": ACCOUNT_NUMBER,
        "claim_form_bank.ifsc": IFSC,
        "claim_form_bank.account_holder": ACCOUNT_HOLDER,
        "clinical.patient_name": PATIENT,
        "bill.bill_number": claim.bill_number,
        "bill.patient_name": PATIENT,
        "bill.grand_total": total,
        "bank.account_number": ACCOUNT_NUMBER,
        "bank.ifsc": IFSC,
        "bank.account_holder": ACCOUNT_HOLDER,
    }
    truth: dict[str, Any] = _jsonable(
        {
            "about": "Ground truth for the tracer acceptance claim. All data is synthetic.",
            "admission_date": claim.admission,
            "discharge_date": claim.discharge,
            "form": {
                "policy_number": MEMBER_POLICY_NUMBER,
                "member_id": MEMBER_MEMBER_ID,
                "patient_name": PATIENT,
                "hospital_name": HOSPITAL,
                "hospital_city": HOSPITAL_CITY,
                "admission_date": claim.admission,
                "discharge_date": claim.discharge,
                "claimed_amount": total,
                "claim_category": "inpatient",
                "cause": "illness",
                "bank_account_number": ACCOUNT_NUMBER,
                "ifsc": IFSC,
                "declaration_accepted": True,
            },
            "files": files,
            "missing_documents": [DocType.PAYMENT_RECEIPT.value],
            "fields": fields,
            "clinical": {
                "diagnoses": [DIAGNOSIS],
                "procedures": [PROCEDURE],
                "hospital_codes": [HOSPITAL_ICD],
                "history_duration": HISTORY_DURATION,
                "duration_predates_inception": False,
            },
            "bill": {
                "bill_number": claim.bill_number,
                "lines": [
                    {
                        "line_id": n,
                        "section": i.section,
                        "head": i.head.value,
                        "date": claim.on(i.day),
                        "description": i.description,
                        "qty": i.qty,
                        "rate": i.rate,
                        "amount": i.amount,
                    }
                    for n, i in enumerate(claim.ordered_items, start=1)
                ],
                "section_totals": [
                    {"section": s, "total": claim.section_total(s)} for s in SECTION_ORDER
                ],
                "grand_total": total,
            },
            "registration_rules": {"R1": True, "R2": True, "R3": True, "R5": True, "R10": True},
            "sheet": sheet,
            "expected": {
                "first_draft": "query",
                "query_items": ["payment receipt"],
                "final_draft": "partial",
                "icd_family": "K35",
                "letters": ["query", "partial"],
                "approver_band": "approver" if sheet["payable"] <= 100000 else "senior_approver",
                "crc_required": True,
                "final_status": "closed",
                "amount_paid": sheet["payable"],
            },
        }
    )
    return truth


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return value


# --- Drawing helpers -------------------------------------------------------------------------

W, H = A4
LEFT, RIGHT = 40.0, W - 40.0
GREY = Color(0.35, 0.35, 0.35)
LIGHT = Color(0.92, 0.92, 0.92)
FOOTER = "Synthetic test document for the ConsintIQ claims pilot. Not a real claim."


def _new_canvas(buffer: io.BytesIO) -> Canvas:
    pdf = Canvas(buffer, pagesize=A4, invariant=True)
    pdf.setAuthor("claims-pilot mockgen")
    return pdf


def _footer(pdf: Canvas, page: str) -> None:
    pdf.setFont("Helvetica", 6.5)
    pdf.setFillColor(GREY)
    pdf.drawString(LEFT, 22, FOOTER)
    pdf.drawRightString(RIGHT, 22, page)
    pdf.setFillColor(black)


def _band(pdf: Canvas, y: float, text: str) -> float:
    """A grey section band with a bold heading. Returns the y below it."""
    pdf.setFillColor(LIGHT)
    pdf.rect(LEFT, y - 14, RIGHT - LEFT, 15, stroke=0, fill=1)
    pdf.setFillColor(black)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(LEFT + 4, y - 10, text)
    return y - 20


def _box_row(
    pdf: Canvas, y: float, cells: list[tuple[str, str, float]], height: float = 26
) -> float:
    """Boxed label/value cells across the page. Each cell: (label, value, width share)."""
    x = LEFT
    total = sum(share for _, _, share in cells)
    for label, value, share in cells:
        width = (RIGHT - LEFT) * share / total
        pdf.rect(x, y - height, width, height, stroke=1, fill=0)
        pdf.setFont("Helvetica", 6.8)
        pdf.setFillColor(GREY)
        pdf.drawString(x + 3, y - 8.5, label)
        pdf.setFillColor(black)
        pdf.setFont("Courier-Bold", 9.5)
        pdf.drawString(x + 3, y - height + 6, value)
        x += width
    return y - height


def _ticks(pdf: Canvas, y: float, label: str, options: list[tuple[str, bool]]) -> float:
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(LEFT + 4, y - 11, label)
    x = LEFT + 4 + pdf.stringWidth(label, "Helvetica", 8.5) + 10
    for text, ticked in options:
        pdf.rect(x, y - 13, 9, 9, stroke=1, fill=0)
        if ticked:
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(x + 1.5, y - 11.5, "X")
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(x + 13, y - 11, text)
        x += 13 + pdf.stringWidth(text, "Helvetica", 8.5) + 14
    return y - 17


def _para(pdf: Canvas, y: float, text: str, *, size: float = 9, indent: float = 0) -> float:
    """Wrapped text. Returns the y below it."""
    pdf.setFont("Helvetica", size)
    width = RIGHT - LEFT - 8 - indent
    words, line = text.split(), ""
    for word in words:
        trial = f"{line} {word}".strip()
        if pdf.stringWidth(trial, "Helvetica", size) > width:
            pdf.drawString(LEFT + 4 + indent, y, line)
            y -= size + 3.5
            line = word
        else:
            line = trial
    if line:
        pdf.drawString(LEFT + 4 + indent, y, line)
        y -= size + 3.5
    return y


def _labelled(pdf: Canvas, y: float, label: str, text: str, *, size: float = 9) -> float:
    pdf.setFont("Helvetica-Bold", size)
    pdf.drawString(LEFT + 4, y, label)
    return _para(pdf, y - size - 4, text, size=size, indent=10) - 4


def _hospital_header(pdf: Canvas, title: str) -> float:
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawCentredString(W / 2, H - 48, HOSPITAL.upper())
    pdf.setFont("Helvetica", 8.5)
    pdf.drawCentredString(W / 2, H - 61, HOSPITAL_ADDRESS)
    pdf.drawCentredString(
        W / 2, H - 72, f"Tel 011-40000000  |  ROHINI ID {HOSPITAL_ROHINI}  |  NABH accredited"
    )
    pdf.setLineWidth(1.2)
    pdf.line(LEFT, H - 80, RIGHT, H - 80)
    pdf.setLineWidth(1)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(W / 2, H - 98, title)
    return H - 110


def _insurer_header(pdf: Canvas, part: str, who: str) -> float:
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(W / 2, H - 44, "PILOT HEALTH INSURANCE CO. LTD. (SYNTHETIC)")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawCentredString(W / 2, H - 60, f"CLAIM FORM - {part}")
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(W / 2, H - 73, f"REIMBURSEMENT CLAIM - TO BE FILLED IN BY THE {who}")
    pdf.setFont("Helvetica-Oblique", 7.5)
    pdf.drawCentredString(
        W / 2,
        H - 84,
        "The issue of this form is not to be taken as an admission of liability.",
    )
    return H - 96


def _save(build: Callable[[Canvas], None]) -> bytes:
    buffer = io.BytesIO()
    pdf = _new_canvas(buffer)
    build(pdf)
    pdf.save()
    return buffer.getvalue()


# --- The documents ---------------------------------------------------------------------------


def claim_form_part_a(claim: TracerClaim) -> bytes:
    total = inr(claim.grand_total)

    def build(pdf: Canvas) -> None:
        y = _insurer_header(pdf, "PART A", "INSURED")
        y = _band(pdf, y, "A. DETAILS OF PRIMARY INSURED")
        y = _box_row(
            pdf,
            y,
            [
                ("a) Policy No.", MEMBER_POLICY_NUMBER, 3),
                ("b) Sl. No. / Certificate No.", "-", 2),
                ("c) Company / TPA ID No. (Member ID)", MEMBER_MEMBER_ID, 3),
            ],
        )
        y = _box_row(pdf, y, [("d) Name", PATIENT.upper(), 3), ("e) Phone", "9000000001", 2)])
        y = _box_row(
            pdf,
            y,
            [
                ("f) Address", "C-42, Malviya Nagar", 4),
                ("City", "New Delhi", 2),
                ("State", "Delhi", 1.4),
                ("Pin code", "110017", 1.4),
            ],
        )
        y = _box_row(pdf, y, [("g) Email ID", MEMBER_EMAIL, 1)]) - 6
        y = _band(pdf, y, "B. DETAILS OF INSURANCE HISTORY")
        y = _ticks(
            pdf,
            y,
            "a) Currently covered by any other mediclaim / health insurance:",
            [("Yes", False), ("No", True)],
        )
        y = (
            _ticks(
                pdf,
                y,
                "b) Hospitalised in the last four years since inception of the policy:",
                [("Yes", False), ("No", True)],
            )
            - 4
        )
        y = _band(pdf, y, "C. DETAILS OF INSURED PERSON HOSPITALISED")
        y = _box_row(
            pdf,
            y,
            [
                ("a) Name", PATIENT.upper(), 3),
                ("c) Age", f"{claim.age} yrs", 1),
                ("d) Date of birth (DD/MM/YYYY)", dmy(PATIENT_DOB), 2),
            ],
        )
        y = _ticks(pdf, y - 2, "b) Gender:", [("Male", False), ("Female", True)])
        y = _ticks(
            pdf,
            y,
            "e) Relationship to primary insured:",
            [
                ("Self", True),
                ("Spouse", False),
                ("Child", False),
                ("Father", False),
                ("Mother", False),
                ("Other", False),
            ],
        )
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("f) Occupation", "Service", 1),
                    ("g) Address (if different)", "Same as above", 2),
                ],
            )
            - 6
        )
        y = _band(pdf, y, "D. DETAILS OF HOSPITALISATION")
        y = _box_row(
            pdf,
            y,
            [("a) Name of hospital where admitted", HOSPITAL, 3), ("City", HOSPITAL_CITY, 1)],
        )
        y = _ticks(
            pdf,
            y - 2,
            "b) Room category occupied:",
            [
                ("Day care", False),
                ("Single occupancy", False),
                ("Twin sharing", False),
                ("3 or more beds", False),
                ("Deluxe", True),
            ],
        )
        y = _ticks(
            pdf,
            y,
            "c) Hospitalisation due to:",
            [("Injury", False), ("Illness", True), ("Maternity", False)],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("d) Date of injury / disease first detected", dmy(claim.on(-2)), 2),
                ("e) Date of admission (DD/MM/YYYY)", dmy(claim.admission), 2),
                ("Time", "10:40", 1),
            ],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("f) Date of discharge (DD/MM/YYYY)", dmy(claim.discharge), 2),
                ("Time", "12:15", 1),
                ("g) System of medicine", "Allopathy", 2),
            ],
        )
        _ticks(
            pdf,
            y - 2,
            "h) If injury: reported to police / medico-legal case:",
            [("Yes", False), ("No", False), ("Not applicable", True)],
        )
        _footer(pdf, "Claim form Part A, page 1 of 2")
        pdf.showPage()

        y = _band(pdf, H - 48, "E. DETAILS OF CLAIM")
        rows = [
            ("i) Pre-hospitalisation expenses", "0.00"),
            ("ii) Hospitalisation expenses", total),
            ("iii) Post-hospitalisation expenses", "0.00"),
            ("iv) Health check-up cost", "0.00"),
            ("v) Ambulance charges", "0.00"),
            ("vi) Others (code): -", "0.00"),
        ]
        for label, amount in rows:
            y = _box_row(pdf, y, [(label, "", 3), ("Amount (Rs.)", amount, 1.4)], height=26)
        y = _box_row(pdf, y, [("TOTAL CLAIMED", "", 3), ("Amount (Rs.)", total, 1.4)], height=26)
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("Pre-hospitalisation period (days)", "0", 1),
                    ("Post-hospitalisation period (days)", "0", 1),
                ],
            )
            - 6
        )
        y = _band(pdf, y, "F. CLAIM DOCUMENTS SUBMITTED - CHECK LIST")
        checks = [
            ("Claim form duly signed", True),
            ("Hospital main bill", True),
            ("Hospital break-up bill", True),
            ("Hospital bill payment receipt", False),
            ("Discharge summary", True),
            ("Pharmacy bills", False),
            ("Investigation reports", False),
            ("Cancelled cheque / NEFT details", True),
        ]
        for i in range(0, len(checks), 2):
            y = _ticks(pdf, y, "", checks[i : i + 2])
        y = _band(pdf, y - 4, "G. DETAILS OF BILLS ENCLOSED")
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("Sl.", "1", 0.5),
                    ("Bill no.", claim.bill_number, 3),
                    ("Date", dmy(claim.discharge), 1.6),
                    ("Issued by", "Hospital", 1.5),
                    ("Towards", "Main bill", 1.6),
                    ("Amount (Rs.)", total, 1.6),
                ],
            )
            - 6
        )
        y = _band(pdf, y, "H. DETAILS OF PRIMARY INSURED'S BANK ACCOUNT (NEFT)")
        y = _box_row(
            pdf,
            y,
            [
                ("a) Account number", ACCOUNT_NUMBER, 2),
                ("b) Bank name and branch", f"{BANK}, {BANK_BRANCH}", 3),
            ],
        )
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("c) Cheque / DD payable details (account holder)", ACCOUNT_HOLDER, 3),
                    ("d) IFSC code", IFSC, 2),
                ],
            )
            - 6
        )
        y = _band(pdf, y, "I. DECLARATION BY THE INSURED")
        y = _para(
            pdf,
            y - 4,
            "I hereby declare that the information furnished in this claim form is true and "
            "correct to the best of my knowledge and belief. If I have made any false or untrue "
            "statement, suppression or concealment of any material fact, my right to claim "
            "reimbursement shall be forfeited. I agree to provide any further information the "
            "insurer may require.",
            size=8.5,
        )
        y -= 10
        pdf.setFont("Helvetica", 9)
        pdf.drawString(LEFT + 4, y, f"Date: {dmy(claim.discharge + timedelta(days=3))}")
        pdf.drawString(LEFT + 4, y - 14, "Place: New Delhi")
        pdf.setFont("Helvetica-Oblique", 13)
        pdf.drawRightString(RIGHT - 10, y - 6, PATIENT)
        pdf.setFont("Helvetica", 8)
        pdf.drawRightString(RIGHT - 10, y - 20, "Signature of the insured")
        _footer(pdf, "Claim form Part A, page 2 of 2")
        pdf.showPage()

    return _save(build)


def claim_form_part_b(claim: TracerClaim) -> bytes:
    def build(pdf: Canvas) -> None:
        y = _insurer_header(pdf, "PART B", "HOSPITAL")
        y = _band(pdf, y, "A. DETAILS OF HOSPITAL")
        y = _box_row(
            pdf,
            y,
            [
                ("a) Name of the hospital", HOSPITAL, 3),
                ("b) Hospital ID (ROHINI)", HOSPITAL_ROHINI, 2),
            ],
        )
        y = _ticks(pdf, y - 2, "c) Type of hospital:", [("Network", True), ("Non network", False)])
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("d) Name of the treating doctor", SURGEON, 2),
                    ("e) Qualification", "MS (General Surgery)", 2),
                ],
            )
            - 6
        )
        y = _band(pdf, y, "B. DETAILS OF THE PATIENT ADMITTED")
        y = _box_row(
            pdf,
            y,
            [
                ("a) Name of the patient", PATIENT.upper(), 3),
                ("b) IP registration number", claim.ip_number, 2),
            ],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("c) Gender", PATIENT_GENDER, 1),
                ("d) Age", f"{claim.age} yrs", 1),
                ("e) Date of birth", dmy(PATIENT_DOB), 1.4),
            ],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("f) Date of admission", dmy(claim.admission), 1),
                ("g) Time", "10:40", 0.6),
                ("h) Date of discharge", dmy(claim.discharge), 1),
                ("i) Time", "12:15", 0.6),
            ],
        )
        y = _ticks(
            pdf,
            y - 2,
            "j) Type of admission:",
            [("Emergency", True), ("Planned", False), ("Day care", False)],
        )
        y = _ticks(
            pdf,
            y,
            "k) Status at time of discharge:",
            [("Discharged to home", True), ("Transferred", False), ("LAMA", False)],
        )
        y = _box_row(pdf, y, [("l) Total claim amount (Rs.)", inr(claim.grand_total), 1)]) - 6
        y = _band(pdf, y, "C. DETAILS OF AILMENT DIAGNOSED (PRIMARY)")
        y = _box_row(pdf, y, [("ICD-10 code", HOSPITAL_ICD, 1), ("Description", DIAGNOSIS, 4)])
        y = (
            _box_row(
                pdf, y, [("Procedure", PROCEDURE, 3), ("Date of procedure", dmy(claim.surgery), 1)]
            )
            - 6
        )
        y = _band(pdf, y, "D. CLAIM DOCUMENTS SUBMITTED")
        y = _ticks(
            pdf,
            y,
            "",
            [
                ("Discharge summary", True),
                ("Final bill", True),
                ("Investigation reports", False),
                ("OT notes", False),
            ],
        )
        y = _band(pdf, y - 4, "E. DECLARATION BY THE HOSPITAL")
        y = _para(
            pdf,
            y - 4,
            "We hereby declare that the information furnished in this form is accurate and "
            "correct to the best of our knowledge and belief. The patient was treated as an "
            "inpatient at our hospital for the period stated above.",
            size=8.5,
        )
        pdf.setFont("Helvetica", 9)
        pdf.drawString(LEFT + 4, y - 16, f"Date: {dmy(claim.discharge)}   Place: New Delhi")
        _seal(pdf, RIGHT - 120, y - 40)
        _footer(pdf, "Claim form Part B, page 1 of 1")
        pdf.showPage()

    return _save(build)


def _seal(pdf: Canvas, x: float, y: float) -> None:
    pdf.setStrokeColor(Color(0.2, 0.3, 0.7))
    pdf.setFillColor(Color(0.2, 0.3, 0.7))
    pdf.circle(x, y, 44, stroke=1, fill=0)
    pdf.circle(x, y, 39, stroke=1, fill=0)
    pdf.setFont("Helvetica-Bold", 5.5)
    pdf.drawCentredString(x, y + 8, "SUNRISE MULTISPECIALITY")
    pdf.drawCentredString(x, y, "HOSPITAL")
    pdf.drawCentredString(x, y - 8, "NEW DELHI")
    pdf.setStrokeColor(black)
    pdf.setFillColor(black)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawCentredString(x, y - 56, "Authorised signatory and seal")


def discharge_summary(claim: TracerClaim) -> bytes:
    def build(pdf: Canvas) -> None:
        y = _hospital_header(pdf, "DISCHARGE SUMMARY")
        y = _box_row(
            pdf,
            y,
            [
                ("Patient name", f"Mrs. {PATIENT.upper()}", 3),
                ("Age / Sex", f"{claim.age} Y / F", 1.3),
                ("UHID", claim.uhid, 1.8),
            ],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("IP No.", claim.ip_number, 1.5),
                ("Date of admission", dmon(claim.admission), 1.5),
                ("Date of discharge", dmon(claim.discharge), 1.5),
                ("Ward / Bed", "Deluxe 3-07", 1),
            ],
        )
        y = (
            _box_row(
                pdf,
                y,
                [
                    ("Consultant", f"{SURGEON}, MS (General Surgery)", 3),
                    ("Department", "General and Laparoscopic Surgery", 3),
                ],
            )
            - 12
        )
        y = _labelled(pdf, y, "FINAL DIAGNOSIS", f"{DIAGNOSIS} (ICD-10: {HOSPITAL_ICD})")
        y = _labelled(
            pdf,
            y,
            "CHIEF COMPLAINTS",
            f"Pain in the right lower abdomen {HISTORY_DURATION}, associated with nausea, "
            "two episodes of vomiting and low grade fever.",
        )
        y = _labelled(
            pdf,
            y,
            "HISTORY OF PRESENT ILLNESS",
            "The patient presented to the emergency department with periumbilical pain that "
            "shifted to the right iliac fossa. Loss of appetite. No history of trauma. No "
            "urinary or bowel complaints.",
        )
        y = _labelled(pdf, y, "PAST HISTORY", "Not significant. No previous surgery.")
        y = _labelled(
            pdf,
            y,
            "EXAMINATION ON ADMISSION",
            "Temperature 100.2 F, pulse 98/min, BP 124/80 mmHg. Abdomen: tenderness and "
            "guarding in the right iliac fossa, maximum at McBurney's point, rebound tenderness "
            "present. Alvarado score 8.",
        )
        y = _labelled(
            pdf,
            y,
            "INVESTIGATIONS",
            "TLC 14,200 /cumm with neutrophilia (82%). CRP 48 mg/L. Serum creatinine 0.8 mg/dL. "
            "USG abdomen: inflamed, non-compressible appendix, 9 mm in diameter, with "
            "periappendiceal fat stranding; no collection, no free fluid.",
        )
        _labelled(
            pdf,
            y,
            "PROCEDURE",
            f"{PROCEDURE} under general anaesthesia on {dmon(claim.surgery)} by {SURGEON}. "
            f"Anaesthetist: {ANAESTHETIST}. Operative findings: acutely inflamed, oedematous "
            "appendix with no perforation, no gangrene and no abscess. Specimen sent for "
            "histopathology.",
        )
        _footer(pdf, "Discharge summary, page 1 of 2")
        pdf.showPage()

        y = _hospital_header(pdf, "DISCHARGE SUMMARY (contd.)")
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(LEFT + 4, y, f"Patient: Mrs. {PATIENT.upper()}   IP No. {claim.ip_number}")
        y -= 20
        y = _labelled(
            pdf,
            y,
            "COURSE IN HOSPITAL",
            "The patient was started on IV antibiotics (ceftriaxone, metronidazole), IV fluids "
            "and analgesics. Laparoscopic appendicectomy was done on the second day of "
            "admission. The post-operative period was uneventful. Oral diet was started on "
            "post-operative day 1. Drain was not placed. Wounds healthy at discharge. "
            "Histopathology: acute suppurative appendicitis.",
        )
        y = _labelled(
            pdf,
            y,
            "CONDITION AT DISCHARGE",
            "Stable, afebrile, ambulatory, tolerating oral diet. Port sites clean.",
        )
        y = _labelled(
            pdf,
            y,
            "MEDICATIONS ON DISCHARGE",
            "1. Tab. Cefuroxime 500 mg twice daily for 5 days. 2. Tab. Paracetamol 650 mg when "
            "required for pain. 3. Tab. Pantoprazole 40 mg once daily before breakfast for 5 days.",
        )
        y = _labelled(
            pdf,
            y,
            "ADVICE",
            "Soft diet. Avoid lifting heavy weights for 4 weeks. Keep port sites dry. Review in "
            "the surgery OPD after 7 days for suture removal, or earlier if fever, pain or "
            "wound discharge.",
        )
        pdf.setFont("Helvetica-Oblique", 12)
        pdf.drawString(LEFT + 10, y - 30, "R. Khanna")
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(LEFT + 10, y - 44, f"{SURGEON}, MS (General Surgery)")
        pdf.drawString(LEFT + 10, y - 56, "Consultant Surgeon, Reg. No. DMC-00000")
        _seal(pdf, RIGHT - 90, y - 50)
        _footer(pdf, "Discharge summary, page 2 of 2")
        pdf.showPage()

    return _save(build)


_COLUMNS = [("Date", LEFT + 4), ("Particulars", LEFT + 78), ("Qty", 352.0)]
_RATE_X, _AMOUNT_X = 440.0, RIGHT - 4


def final_bill(claim: TracerClaim) -> bytes:
    def header(pdf: Canvas, contd: bool) -> float:
        title = "FINAL BILL - INPATIENT (DETAILED)" + (" contd." if contd else "")
        y = _hospital_header(pdf, title)
        y = _box_row(
            pdf,
            y,
            [
                ("Bill No.", claim.bill_number, 2),
                ("Bill date", dmon(claim.discharge), 1.3),
                ("UHID", claim.uhid, 1.6),
                ("IP No.", claim.ip_number, 1.4),
            ],
        )
        if not contd:
            y = _box_row(
                pdf,
                y,
                [
                    ("Patient name", f"Mrs. {PATIENT.upper()}", 2.6),
                    ("Age / Sex", f"{claim.age} Y / F", 1),
                    ("Payer", "Self (reimbursement)", 1.6),
                ],
            )
            y = _box_row(
                pdf,
                y,
                [
                    ("Date of admission", dmon(claim.admission), 1),
                    ("Date of discharge", dmon(claim.discharge), 1),
                    ("Room category", "Deluxe Single", 1),
                    ("Consultant", SURGEON, 1.2),
                ],
            )
        y -= 8
        pdf.setFillColor(LIGHT)
        pdf.rect(LEFT, y - 14, RIGHT - LEFT, 15, stroke=0, fill=1)
        pdf.setFillColor(black)
        pdf.setFont("Helvetica-Bold", 8.5)
        for name, x in _COLUMNS:
            pdf.drawString(x, y - 10, name)
        pdf.drawRightString(_RATE_X + 30, y - 10, "Rate (Rs.)")
        pdf.drawRightString(_AMOUNT_X, y - 10, "Amount (Rs.)")
        return y - 28

    def section(pdf: Canvas, y: float, name: str) -> float:
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(LEFT + 4, y, name)
        y -= 14
        pdf.setFont("Helvetica", 8.8)
        for item in sorted(claim.section_items(name), key=lambda i: i.day):
            pdf.drawString(LEFT + 4, y, dmon(claim.on(item.day)))
            pdf.drawString(LEFT + 78, y, item.description)
            pdf.drawRightString(372, y, f"{item.qty:g}")
            pdf.drawRightString(_RATE_X + 30, y, inr(item.rate))
            pdf.drawRightString(_AMOUNT_X, y, inr(item.amount))
            y -= 13
        pdf.setLineWidth(0.5)
        pdf.line(_RATE_X - 20, y + 8, RIGHT, y + 8)
        pdf.setFont("Helvetica-Bold", 8.8)
        pdf.drawRightString(_RATE_X + 30, y - 2, "Section total")
        pdf.drawRightString(_AMOUNT_X, y - 2, inr(claim.section_total(name)))
        pdf.setLineWidth(1)
        return y - 20

    def build(pdf: Canvas) -> None:
        y = header(pdf, contd=False)
        for name in SECTION_ORDER:
            if name in PAGE_ONE_SECTIONS:
                y = section(pdf, y, name)
        _footer(pdf, "Final bill, page 1 of 2")
        pdf.showPage()
        y = header(pdf, contd=True)
        for name in SECTION_ORDER:
            if name not in PAGE_ONE_SECTIONS:
                y = section(pdf, y, name)
        total = claim.grand_total
        pdf.setLineWidth(1.2)
        pdf.line(LEFT, y + 4, RIGHT, y + 4)
        pdf.setLineWidth(1)
        rows = [
            ("Gross total", total),
            ("Less: discount", D("0")),
            ("Less: advance received", D("0")),
            ("NET AMOUNT PAYABLE", total),
        ]
        y -= 12
        for label, amount in rows:
            bold = label.isupper()
            pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
            pdf.drawRightString(_RATE_X + 30, y, label)
            pdf.drawRightString(_AMOUNT_X, y, inr(amount))
            y -= 15
        pdf.setFont("Helvetica-Oblique", 8.5)
        pdf.drawString(LEFT + 4, y - 4, f"Amount in words: {rupees_in_words(total)}")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(
            LEFT + 4, y - 20, "All charges are inclusive of applicable taxes. E. and O.E."
        )
        _seal(pdf, RIGHT - 90, y - 70)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(LEFT + 4, y - 60, "Prepared by: Billing desk (IP)")
        pdf.drawString(LEFT + 4, y - 72, "Checked by: Accounts")
        _footer(pdf, "Final bill, page 2 of 2")
        pdf.showPage()

    return _save(build)


def cancelled_cheque(claim: TracerClaim) -> bytes:
    def build(pdf: Canvas) -> None:
        x0, y0, w, h = LEFT + 10, H - 330, RIGHT - LEFT - 20, 240
        pdf.setFillColor(Color(0.93, 0.96, 0.99))
        pdf.rect(x0, y0, w, h, stroke=1, fill=1)
        pdf.setFillColor(black)
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(x0 + 14, y0 + h - 24, BANK.upper())
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(x0 + 14, y0 + h - 37, f"Branch: {BANK_BRANCH}")
        pdf.drawString(x0 + 14, y0 + h - 49, f"IFSC: {IFSC}")
        pdf.drawRightString(x0 + w - 14, y0 + h - 24, "Date  _ _ / _ _ / _ _ _ _")
        pdf.drawString(x0 + 14, y0 + h - 80, "Pay")
        pdf.line(x0 + 40, y0 + h - 82, x0 + w - 90, y0 + h - 82)
        pdf.drawRightString(x0 + w - 14, y0 + h - 80, "or Bearer")
        pdf.drawString(x0 + 14, y0 + h - 104, "Rupees")
        pdf.line(x0 + 56, y0 + h - 106, x0 + w - 150, y0 + h - 106)
        pdf.rect(x0 + w - 140, y0 + h - 114, 126, 20, stroke=1, fill=0)
        pdf.drawString(x0 + w - 136, y0 + h - 108, "Rs.")
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(x0 + 14, y0 + h - 136, f"A/c No. {ACCOUNT_NUMBER}")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(x0 + 14, y0 + h - 150, "SB - Savings account")
        pdf.drawRightString(x0 + w - 14, y0 + 72, ACCOUNT_HOLDER)
        pdf.line(x0 + w - 170, y0 + 84, x0 + w - 14, y0 + 84)
        pdf.drawRightString(x0 + w - 14, y0 + 62, "Please sign above")
        pdf.setFont("Courier-Bold", 12)
        pdf.drawString(x0 + 60, y0 + 16, "c004217c 110419027c 000417c 31")
        pdf.saveState()
        pdf.setStrokeColor(Color(0.75, 0.1, 0.1))
        pdf.setFillColor(Color(0.75, 0.1, 0.1))
        pdf.setLineWidth(2)
        pdf.translate(x0 + w / 2 + 40, y0 + h / 2 + 34)
        pdf.rotate(12)
        pdf.line(-130, 16, 130, 16)
        pdf.line(-130, -18, 130, -18)
        pdf.setFont("Helvetica-Bold", 30)
        pdf.drawCentredString(0, -10, "CANCELLED")
        pdf.restoreState()
        _footer(pdf, "Cancelled cheque")
        pdf.showPage()

    return _save(build)


def payment_receipt(claim: TracerClaim) -> bytes:
    total = claim.grand_total

    def build(pdf: Canvas) -> None:
        y = _hospital_header(pdf, "PAYMENT RECEIPT")
        y = _box_row(
            pdf,
            y,
            [
                ("Receipt No.", claim.receipt_number, 2),
                ("Receipt date", dmon(claim.discharge), 1),
                ("UHID", claim.uhid, 1.4),
            ],
        )
        y = _box_row(
            pdf,
            y,
            [
                ("Received from", f"Mrs. {PATIENT.upper()}", 2),
                ("IP No.", claim.ip_number, 1),
                ("Against bill No.", claim.bill_number, 1.6),
            ],
        )
        y -= 20
        y = _para(
            pdf,
            y,
            f"Received with thanks from Mrs. {PATIENT} the sum of Rs. {inr(total)} "
            f"({rupees_in_words(total)}) towards full and final settlement of inpatient bill "
            f"No. {claim.bill_number} dated {dmon(claim.discharge)}.",
            size=10,
        )
        y -= 10
        y = _box_row(
            pdf,
            y,
            [
                ("Payment mode", "UPI", 1),
                ("Transaction reference", f"UPI/{claim.discharge:%Y%m%d}/7731{claim.serial}", 2),
                ("Amount received (Rs.)", inr(total), 1.4),
            ],
        )
        y = _box_row(
            pdf, y, [("Bill amount (Rs.)", inr(total), 1), ("Balance due (Rs.)", "0.00", 1)]
        )
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(LEFT + 4, y - 30, "PAID")
        _seal(pdf, RIGHT - 90, y - 80)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(LEFT + 4, y - 60, "Cashier: IP billing counter")
        _footer(pdf, "Payment receipt")
        pdf.showPage()

    return _save(build)


# --- Realism pass and merging ----------------------------------------------------------------

SCAN_DPI = 150


def scanned(pdf_bytes: bytes, page_index: int, *, angle: float, seed: int) -> bytes:
    """The document with one page replaced by a scan-like image: slightly rotated, with
    paper tone and speckle. The page keeps no text layer, like a real scan."""
    source = pdfium.PdfDocument(pdf_bytes)
    try:
        image = source[page_index].render(scale=SCAN_DPI / 72).to_pil().convert("L")
        rng = random.Random(seed)
        image = image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        noise = Image.effect_noise(image.size, 22)
        image = Image.blend(image, noise, 0.07)
        image = ImageChops.multiply(image, Image.new("L", image.size, 244))  # paper tone
        draw = ImageDraw.Draw(image)
        for _ in range(900):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            draw.point((x, y), fill=rng.randrange(60, 170))
        jpeg = io.BytesIO()
        image.save(jpeg, format="JPEG", quality=72)
        page = io.BytesIO()
        canvas = _new_canvas(page)
        canvas.setFillColor(white)
        canvas.drawImage(ImageReader(io.BytesIO(jpeg.getvalue())), 0, 0, width=W, height=H)
        canvas.showPage()
        canvas.save()
        replacement = pdfium.PdfDocument(page.getvalue())
        out = pdfium.PdfDocument.new()
        try:
            for i in range(len(source)):
                if i == page_index:
                    out.import_pages(replacement, [0])
                else:
                    out.import_pages(source, [i])
            return _pdf_bytes(out)
        finally:
            out.close()
            replacement.close()
    finally:
        source.close()


def merge(parts: list[bytes]) -> bytes:
    out = pdfium.PdfDocument.new()
    try:
        for part in parts:
            doc = pdfium.PdfDocument(part)
            try:
                out.import_pages(doc)
            finally:
                doc.close()
        return _pdf_bytes(out)
    finally:
        out.close()


def _pdf_bytes(doc: Any) -> bytes:
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def page_count(data: bytes) -> int:
    doc = pdfium.PdfDocument(data)
    try:
        return len(doc)
    finally:
        doc.close()


# --- Bundle ----------------------------------------------------------------------------------

BUNDLE: list[tuple[str, DocType, Callable[[TracerClaim], bytes]]] = [
    ("01_claim_form_part_a.pdf", DocType.CLAIM_FORM_PART_A, claim_form_part_a),
    ("02_claim_form_part_b.pdf", DocType.CLAIM_FORM_PART_B, claim_form_part_b),
    ("03_discharge_summary.pdf", DocType.DISCHARGE_SUMMARY, discharge_summary),
    ("04_final_bill.pdf", DocType.FINAL_BILL, final_bill),
    ("05_cancelled_cheque.pdf", DocType.CANCELLED_CHEQUE, cancelled_cheque),
]
# (file, page index, angle in degrees): the pages that get the scan treatment.
SCANS = {"03_discharge_summary.pdf": (1, 0.8), "05_cancelled_cheque.pdf": (0, -1.3)}
MERGED = "bundle_merged.pdf"
REPLY = "query_reply_payment_receipt.pdf"
GROUND_TRUTH = "ground_truth.json"


def generate(out_dir: Path, admission: date) -> dict[str, Any]:
    """Write the bundle, the reply and the ground truth. Returns the ground truth."""
    out_dir.mkdir(parents=True, exist_ok=True)
    claim = TracerClaim(admission=admission)
    parts: list[bytes] = []
    files: dict[str, Any] = {"bundle": [], "merged": MERGED, "reply": None}
    pages: list[str] = []
    for i, (name, doc_type, render) in enumerate(BUNDLE):
        data = render(claim)
        if name in SCANS:
            index, angle = SCANS[name]
            data = scanned(data, index, angle=angle, seed=claim.admission.toordinal() + i)
        (out_dir / name).write_bytes(data)
        count = page_count(data)
        files["bundle"].append(
            {
                "file": name,
                "doc_type": doc_type.value,
                "pages": count,
                "scanned_page": SCANS[name][0] + 1 if name in SCANS else None,
            }
        )
        pages += [doc_type.value] * count
        parts.append(data)
    (out_dir / MERGED).write_bytes(merge(parts))
    receipt = payment_receipt(claim)
    (out_dir / REPLY).write_bytes(receipt)
    files["reply"] = {
        "file": REPLY,
        "doc_type": DocType.PAYMENT_RECEIPT.value,
        "pages": page_count(receipt),
    }
    files["merged_pages"] = pages
    rules = DefaultRulesEngine(REPO / "config" / "products")
    truth = ground_truth(claim, rules, files)
    (out_dir / GROUND_TRUTH).write_text(json.dumps(truth, indent=2) + "\n")
    return truth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--admission-date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=21),
        help="admission date, YYYY-MM-DD (default: 21 days ago)",
    )
    args = parser.parse_args()
    truth = generate(args.out, args.admission_date)
    sheet = truth["sheet"]
    print(f"Wrote the tracer bundle to {args.out}")
    print(f"  admission {truth['admission_date']}, discharge {truth['discharge_date']}")
    print(f"  pages in {MERGED}: {len(truth['files']['merged_pages'])}")
    print(
        f"  claimed Rs {sheet['claimed_total']}, admissible Rs {sheet['admissible_total']}, "
        f"co-pay Rs {sheet['co_pay']}, payable Rs {sheet['payable']}"
    )
    for line in sheet["deductions"]:
        print(
            f"  line {line['line_id']:>2} {line['rule_id']:<3} {line['clause_ref']:<8} "
            f"-Rs {line['deducted']:>9}  {line['description']}"
        )


if __name__ == "__main__":
    main()
