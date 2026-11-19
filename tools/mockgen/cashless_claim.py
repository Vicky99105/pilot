"""Generate the cashless acceptance scenario: one admission at a CGHS network hospital, as
three hospital uploads (pre-auth, enhancement, discharge), plus the ground truth a live run
is scored against.

Run from the repo root:

    uv run --project backend python tools/mockgen/cashless_claim.py --out <dir>
    uv run --project backend python tools/mockgen/cashless_claim.py --out <dir> \
        --admission-date 2026-11-14

The admission:
- Patient: Imran Qureshi, policy 2 of the mock core seed (MOCK/AS/2026/000102), sum insured
  Rs 3 lakh. The hospital desk user of Sunrise sends every request.
- Hospital: Sunrise Multispeciality Hospital, a CGHS network hospital (NABH, X tier,
  semi-private ward). The route is cashless, so its agreement binds: the laparoscopic
  appendicectomy maps to CGHS AG095 at Rs 33,000, which caps the heads the package covers
  (R20), and non-package lines take its 10% discount (R24).
- Pre-auth: Part C with an estimate of Rs 28,000 by head (below the package rate) and the
  ultrasound report. Approved less the 5% co-pay.
- Enhancement on day 3: post-operative ileus, one more day; Rs 7,000 more by head, with the
  interim bill. The revised estimate (Rs 35,000) is capped at the package rate, so the
  approved total rises only to the package rate less co-pay.
- Discharge on day 4: discharge summary, OT notes, histopathology report and the itemised
  final bill. The package heads exceed the rate (R20); gloves are non-payable (R11); an
  ambulance is outside the package (R24 discount, R19 limit). The admissible total exceeds
  what the pre-auth and enhancement approved, so R26 takes the excess and the hospital is
  paid the approved amount.

Every expected amount comes from the real rules engine, run on the ground-truth lines in
bill review's order (R11, R12, R18, R23, R20, R24, R13, R14, R15, R19, R16, R17, R26, then R21),
the estimate phases on the estimate heads and the discharge on the bill. All data is
synthetic.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "backend"):  # run as a script: import the tracer and the app
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from reportlab.lib.colors import black  # noqa: E402
from reportlab.pdfgen.canvas import Canvas  # noqa: E402

from app.services.rules.contract import (  # noqa: E402
    ClaimRoute,
    Facts,
    Head,
    LineFacts,
    NonPayableItem,
    PackageFacts,
    PreAuthFacts,
    RoomFacts,
    RulesEngine,
)
from app.services.rules.service import DefaultRulesEngine  # noqa: E402
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS, PRODUCT_UIN  # noqa: E402
from app.shared.types import DocType  # noqa: E402
from tools.mockgen import tracer_claim as base  # noqa: E402

D = Decimal
ZERO = D("0.00")

# Policy 2 in backend/app/mock_core/seed.py. tests/tools checks these against the seed.
POLICY_NUMBER = "MOCK/AS/2026/000102"
MEMBER_ID = "MOCK-M-000102-01"
PATIENT = "Imran Qureshi"
PATIENT_DOB = date(1990, 9, 30)
PATIENT_GENDER = "Male"
SUM_INSURED = D("300000")
HOSPITAL = base.HOSPITAL
HOSPITAL_CITY = base.HOSPITAL_CITY
HOSPITAL_ROHINI = HOSPITAL_ROHINI_IDS[0]
DIFFERENTIAL_BILLING = True
SURGEON = base.SURGEON
SURGEON_REGISTRATION = "DMC/R/04512"
DIAGNOSIS = "Acute appendicitis"
FINAL_DIAGNOSIS = "Acute appendicitis with post-operative paralytic ileus"
PROCEDURE = "Laparoscopic appendicectomy"
HOSPITAL_ICD = "K35.8"
COMPLAINTS = "Pain in the right lower abdomen since 2 days, nausea, vomiting and fever"
EXPECTED_STAY = 3
STAY_DAYS = 4  # one more day after the enhancement
ROUTE: ClaimRoute = "cashless"
PACKAGE = base.PACKAGE  # CGHS AG095 at Rs 33,000 under Sunrise's agreement
NON_PACKAGE_DISCOUNT = base.NON_PACKAGE_DISCOUNT
PCS_CODE = base.PCS_CODE
ICD_FAMILY = "K35"
GUIDELINE_CODE = base.GUIDELINE_CODE
NON_PAYABLE_ITEM = base.NON_PAYABLE_ITEM  # Gloves: non-payable list I


@dataclass(frozen=True)
class Head_:
    """An estimate head as the hospital writes it."""

    name: str
    head: Head
    amount: Decimal
    days: Decimal | None = None


PREAUTH_ESTIMATE = [
    Head_("Room rent (semi-private)", Head.ROOM, D("12000.00"), D("3")),
    Head_("OT charges", Head.OT, D("8000.00")),
    Head_("Surgeon and anaesthetist fees", Head.PROFESSIONAL_FEES, D("5000.00")),
    Head_("Medicines", Head.PHARMACY, D("3000.00")),
]
ENHANCEMENT_ESTIMATE = [
    Head_("Room rent (semi-private)", Head.ROOM, D("4000.00"), D("1")),
    Head_("Medicines", Head.PHARMACY, D("3000.00")),
]
ENHANCEMENT_REASON = (
    "Post-operative paralytic ileus on day 2: abdominal distension, no bowel sounds. "
    "Nil by mouth, IV fluids and nasogastric decompression; one more day of stay."
)


@dataclass(frozen=True)
class Item:
    section: str
    head: Head
    day: int
    description: str
    qty: Decimal
    rate: Decimal

    @property
    def amount(self) -> Decimal:
        return base.money(self.qty * self.rate)


ROOM, OT, FEES, PHARMACY = "ROOM CHARGES", "OT CHARGES", "PROFESSIONAL FEES", "PHARMACY"
CONSUMABLES, INVESTIGATIONS, AMBULANCE = "CONSUMABLES", "INVESTIGATIONS", "AMBULANCE"
SECTION_ORDER = [ROOM, OT, FEES, PHARMACY, CONSUMABLES, INVESTIGATIONS, AMBULANCE]


def _items() -> list[Item]:
    one = D("1")
    items = [Item(ROOM, Head.ROOM, d, "Semi-private Room Rent", one, D("4000")) for d in range(4)]
    items += [
        Item(OT, Head.OT, 1, "Operation Theatre Charges - Major (up to 2 hrs)", one, D("8500")),
        Item(FEES, Head.PROFESSIONAL_FEES, 1, f"Surgeon Fee - {SURGEON}", one, D("6000")),
        Item(
            FEES,
            Head.PROFESSIONAL_FEES,
            1,
            f"Anaesthetist Fee - {base.ANAESTHETIST}",
            one,
            D("2500"),
        ),
        Item(PHARMACY, Head.PHARMACY, 0, "Inj. Ceftriaxone 1 g", D("8"), D("68.50")),
        Item(PHARMACY, Head.PHARMACY, 0, "IV Fluid Ringer Lactate 500 ml", D("14"), D("46.00")),
        Item(PHARMACY, Head.PHARMACY, 2, "Inj. Pantoprazole 40 mg", D("6"), D("54.00")),
        Item(
            CONSUMABLES, Head.CONSUMABLES, 1, "Laparoscopic Trocar Kit (disposable)", one, D("4850")
        ),
        Item(CONSUMABLES, Head.CONSUMABLES, 1, NON_PAYABLE_ITEM, D("10"), D("22.00")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "Ultrasound Abdomen and Pelvis", one, D("1900")),
        Item(INVESTIGATIONS, Head.DIAGNOSTICS, 0, "Complete Blood Count (CBC)", one, D("420")),
        Item(
            INVESTIGATIONS,
            Head.DIAGNOSTICS,
            2,
            "Histopathology - Appendix Specimen",
            one,
            D("1450"),
        ),
        Item(AMBULANCE, Head.AMBULANCE, 0, "Ambulance to hospital", one, D("1500")),
    ]
    return items


@dataclass(frozen=True)
class CashlessClaim:
    admission: date

    @property
    def discharge(self) -> date:
        return self.admission + timedelta(days=STAY_DAYS)

    @property
    def enhancement_day(self) -> date:
        return self.admission + timedelta(days=2)

    def on(self, day: int) -> date:
        return self.admission + timedelta(days=day)

    @property
    def serial(self) -> str:
        return f"{(self.admission.toordinal() * 7) % 100000:05d}"

    @property
    def bill_number(self) -> str:
        return f"SMH/IP/{self.admission:%y%m}/C{self.serial}"

    @property
    def ip_number(self) -> str:
        return f"IP{self.admission:%y}-C{self.serial}"

    @property
    def age(self) -> int:
        a, b = self.admission, PATIENT_DOB
        return a.year - b.year - ((a.month, a.day) < (b.month, b.day))

    @property
    def items(self) -> list[Item]:
        """Bill order: by section, then by date."""
        everything = _items()
        return [
            i
            for s in SECTION_ORDER
            for i in sorted((x for x in everything if x.section == s), key=lambda x: x.day)
        ]

    @property
    def grand_total(self) -> Decimal:
        return base.money(sum((i.amount for i in self.items), ZERO))


def _estimate_total(heads: list[Head_]) -> Decimal:
    return base.money(sum((h.amount for h in heads), ZERO))


# --- Ground truth: the rules engine in bill review's order ---------------------------------


@dataclass
class Line:
    line_id: int
    head: Head
    description: str
    qty: Decimal | None
    rate: Decimal | None
    claimed: Decimal
    service_date: date | None


def estimate_lines(*phases: list[Head_]) -> list[Line]:
    """Bill review's lines for a request before discharge: the heads in order, days as qty."""
    lines: list[Line] = []
    for heads in phases:
        for h in heads:
            rate = h.amount / h.days if h.days else None
            lines.append(Line(len(lines) + 1, h.head, h.name, h.days, rate, h.amount, None))
    return lines


def bill_lines(claim: CashlessClaim) -> list[Line]:
    return [
        Line(n, i.head, i.description, i.qty, i.rate, i.amount, claim.on(i.day))
        for n, i in enumerate(claim.items, start=1)
    ]


def expected_sheet(
    lines: list[Line],
    rules: RulesEngine,
    *,
    admission: date,
    discharge: date | None,
    preauth: PreAuthFacts | None = None,
    non_payable: list[NonPayableItem] | None = None,
) -> dict[str, Any]:
    """The deduction sheet bill review should write for these lines."""
    product = rules.product(PRODUCT_UIN)
    non_payable = base._non_payable() if non_payable is None else non_payable
    sheet = [
        {
            "line_id": ln.line_id,
            "head": ln.head.value,
            "description": ln.description,
            "claimed": ln.claimed,
            "admissible": ln.claimed,
            "deducted": ZERO,
            "rule_id": None,
            "clause_ref": None,
            "reason": None,
            "deductions": [],
        }
        for ln in lines
    ]
    facts = {ln.line_id: ln for ln in lines}
    results: list[Any] = []
    route = {"claim_route": ROUTE}

    def line_facts(row: dict[str, Any]) -> LineFacts:
        ln = facts[row["line_id"]]
        rate = ln.rate
        if row["deductions"] and ln.qty:
            rate = row["admissible"] / ln.qty
        return LineFacts(
            line_id=ln.line_id,
            head=ln.head,
            description=ln.description,
            qty=ln.qty,
            rate=rate,
            claimed=row["admissible"],
            service_date=ln.service_date,
        )

    def apply(row: dict[str, Any], rule_id: str, extra: dict[str, Any]) -> Any:
        [result] = rules.evaluate([rule_id], Facts(line=line_facts(row), **extra), product)
        results.append(result)
        base._deduct(row, result, -result.amount_effect)
        return result

    def spread(rule_id: str, targets: list[dict[str, Any]], extra: dict[str, Any]) -> Any:
        [result] = rules.evaluate([rule_id], Facts(**extra), product)
        results.append(result)
        if result.amount_effect and targets:
            weights = [row["admissible"] for row in targets]
            for row, share in zip(
                targets, rules.allocate(result.amount_effect, weights), strict=True
            ):
                base._deduct(row, result, -share)
        return result

    def payable() -> list[dict[str, Any]]:
        return [row for row in sheet if row["admissible"] > 0]

    def admissible() -> Decimal:
        return base.money(sum((row["admissible"] for row in sheet), ZERO))

    for row in sheet:  # 1. R11; R12 reads the remaining lines as payable; R18
        apply(row, "R11", {"non_payable": non_payable})
    for row in payable():
        apply(row, "R18", {"admission_date": admission, "discharge_date": discharge})
    package = PackageFacts(packages=[PACKAGE], billed_as_package=False)  # 2. R23, R20
    bound = ROUTE in product.packages.pricing_applies_to
    covered_heads = product.packages.included_heads if bound else []
    for row in payable():
        apply(row, "R23", {"package": package, **route})
    covered = [row for row in payable() if Head(row["head"]) in covered_heads]
    total = sum((row["admissible"] for row in covered), ZERO)
    r20 = spread(
        "R20", covered, {"package": package.model_copy(update={"package_total": total}), **route}
    )
    for row in payable():  # 3. R24, then R13, R14 and R15
        apply(
            row,
            "R24",
            {"package": package, "non_package_discount_percent": NON_PACKAGE_DISCOUNT, **route},
        )
    room_rows = [
        row
        for row in payable()
        if row["head"] == Head.ROOM.value and Head.ROOM not in covered_heads
    ]
    days = sum((facts[r["line_id"]].qty or D("1") for r in room_rows), ZERO)
    rate = sum((r["admissible"] for r in room_rows), ZERO) / days if room_rows else None
    room = RoomFacts(
        sum_insured=SUM_INSURED, room_rate_per_day=rate, differential_billing=DIFFERENTIAL_BILLING
    )
    for row in payable():
        for rule_id in ("R13", "R14", "R15"):
            apply(row, rule_id, {"room": room, "package": package, **route})
    ambulance = [row for row in payable() if row["head"] == Head.AMBULANCE.value]  # 4. claim
    if ambulance:
        spread(
            "R19", ambulance, {"ambulance_total": sum((r["admissible"] for r in ambulance), ZERO)}
        )
    r26 = None
    for rule_id in ("R16", "R17", "R26"):
        result = spread(
            rule_id,
            payable(),
            {
                "sum_insured": SUM_INSURED,
                "admissible_total": admissible(),
                "procedure_codes": [PCS_CODE],
                "diagnosis_codes": [f"{ICD_FAMILY}.80"],
                "preauth": preauth,
            },
        )
        r26 = result if rule_id == "R26" else r26
    [r21] = rules.evaluate(["R21"], Facts(admissible_total=admissible()), product)
    co_pay = base.money(-r21.amount_effect)
    for row in sheet:
        row["deductions"] = [
            {"rule_id": r, "clause_ref": c, "amount": a} for a, r, c, _ in row["deductions"]
        ]
    assert r26 is not None
    return {
        "claim_route": ROUTE,
        "package": PACKAGE.model_dump(mode="json"),
        "lines": sheet,
        "deductions": [row for row in sheet if row["deducted"] > 0],
        "claimed_total": base.money(sum((ln.claimed for ln in lines), ZERO)),
        "admissible_total": admissible(),
        "co_pay": co_pay,
        "payable": base.money(admissible() - co_pay),
        "r20": {"passed": r20.passed, "amount_effect": base.money(r20.amount_effect)},
        "r26": {"passed": r26.passed, "amount_effect": base.money(r26.amount_effect)},
        "rules_failed": sorted({r.rule_id for r in results if not r.passed}),
    }


def ground_truth(claim: CashlessClaim, rules: RulesEngine, files: dict[str, Any]) -> Any:
    preauth = expected_sheet(
        estimate_lines(PREAUTH_ESTIMATE), rules, admission=claim.admission, discharge=None
    )
    enhanced = expected_sheet(
        estimate_lines(PREAUTH_ESTIMATE, ENHANCEMENT_ESTIMATE),
        rules,
        admission=claim.admission,
        discharge=None,
    )
    approved = max(preauth["payable"], enhanced["payable"])
    approved_admissible = max(preauth["admissible_total"], enhanced["admissible_total"])
    final = expected_sheet(
        bill_lines(claim),
        rules,
        admission=claim.admission,
        discharge=claim.discharge,
        preauth=PreAuthFacts(
            approved_total=approved_admissible, diagnosis_codes=[f"{ICD_FAMILY}.80"]
        ),
    )

    def decision(sheet: dict[str, Any]) -> str:
        return "partial" if sheet["deductions"] else "approve"

    fields = {
        "identity.patient_name": PATIENT,
        "identity.policy_number": POLICY_NUMBER,
        "identity.member_id": MEMBER_ID,
        "identity.date_of_birth": PATIENT_DOB,
        "hospital.name": HOSPITAL,
        "admission.admission_date": claim.admission,
        "preauth.rohini_id": HOSPITAL_ROHINI,
        "preauth.treating_doctor": SURGEON,
        "preauth.doctor_registration_number": SURGEON_REGISTRATION,
        "preauth.provisional_diagnosis": DIAGNOSIS,
        "preauth.proposed_treatment": PROCEDURE,
        "preauth.icd_code_written": HOSPITAL_ICD,
        "preauth.admission_type": "emergency",
        "preauth.expected_stay_days": EXPECTED_STAY,
        "preauth.estimate_total": _estimate_total(PREAUTH_ESTIMATE),
        "enhancements.0.extra_amount": _estimate_total(ENHANCEMENT_ESTIMATE),
        "enhancements.0.extra_days": 1,
        "clinical.discharge_date": claim.discharge,
        "bill.bill_number": claim.bill_number,
        "bill.grand_total": claim.grand_total,
    }
    return base._jsonable(
        {
            "about": "Ground truth for the cashless acceptance admission. All data is synthetic.",
            "admission_date": claim.admission,
            "discharge_date": claim.discharge,
            "form": {
                "policy_number": POLICY_NUMBER,
                "member_id": MEMBER_ID,
                "patient_name": PATIENT,
                "hospital_id": HOSPITAL_ROHINI,
                "hospital_name": HOSPITAL,
                "admission_date": claim.admission,
                "claimed_amount": _estimate_total(PREAUTH_ESTIMATE),
            },
            "files": files,
            "fields": fields,
            "requests": {
                "preauth": {
                    "sheet": preauth,
                    "draft": decision(preauth),
                    "approved_now": preauth["payable"],
                    "approved_total": preauth["payable"],
                    "clock_hours": 1,
                },
                "enhancement": {
                    "sheet": enhanced,
                    "draft": decision(enhanced),
                    "approved_now": approved - preauth["payable"],
                    "approved_total": approved,
                    "clock_hours": 1,
                },
                "discharge": {
                    "sheet": final,
                    "draft": decision(final),
                    "approved_total": final["payable"],
                    "clock_hours": 3,
                },
            },
            "expected": {
                "package": PACKAGE.package_code,
                "package_rate": PACKAGE.rate,
                "guideline_code": GUIDELINE_CODE,
                "icd_family": ICD_FAMILY,
                "pcs_family": "0DTJ",
                "approved_admissible": approved_admissible,
                "r26_variance": final["r26"]["amount_effect"],
                "letters": ["authorisation", "authorisation", "authorisation"],
                "payee": {"kind": "hospital", "name": HOSPITAL},
                "amount_paid": final["payable"],
                "final_status": "closed",
            },
        }
    )


# --- The documents ----------------------------------------------------------------------------


def _patient_rows(pdf: Canvas, y: float, claim: CashlessClaim) -> float:
    y = base._box_row(
        pdf,
        y,
        [
            ("Patient name", f"Mr. {PATIENT.upper()}", 3),
            ("Age / Sex", f"{claim.age} Y / M", 1.3),
            ("IP No.", claim.ip_number, 1.8),
        ],
    )
    return y - 10


def _document(
    title: str, claim: CashlessClaim, sections: list[tuple[str, str]], footer: str
) -> bytes:
    def build(pdf: Canvas) -> None:
        y = _patient_rows(pdf, base._hospital_header(pdf, title), claim)
        for label, text in sections:
            y = base._labelled(pdf, y, label, text)
        pdf.setFont("Helvetica-Oblique", 12)
        pdf.drawString(base.LEFT + 10, y - 26, "R. Khanna")
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(base.LEFT + 10, y - 40, f"{SURGEON}, MS (General Surgery)")
        base._seal(pdf, base.RIGHT - 90, y - 50)
        base._footer(pdf, footer)
        pdf.showPage()

    return base._save(build)


def _heads_table(pdf: Canvas, y: float, heads: list[Head_], total_label: str) -> float:
    pdf.setFont("Helvetica-Bold", 8.8)
    pdf.drawString(base.LEFT + 4, y, "Head")
    pdf.drawRightString(380, y, "Days")
    pdf.drawRightString(base.RIGHT - 4, y, "Amount (Rs.)")
    y -= 14
    pdf.setFont("Helvetica", 8.8)
    for h in heads:
        pdf.drawString(base.LEFT + 4, y, h.name)
        pdf.drawRightString(380, y, f"{h.days:g}" if h.days else "")
        pdf.drawRightString(base.RIGHT - 4, y, base.inr(h.amount))
        y -= 13
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawRightString(380, y - 2, total_label)
    pdf.drawRightString(base.RIGHT - 4, y - 2, base.inr(_estimate_total(heads)))
    return y - 22


def preauth_form(claim: CashlessClaim) -> bytes:
    """Part C: the cashless request, filled by the hospital and the treating doctor."""

    def build(pdf: Canvas) -> None:
        y = base._hospital_header(pdf, "REQUEST FOR CASHLESS HOSPITALISATION - PART C")
        y = base._box_row(
            pdf,
            y,
            [
                ("Name of the patient", PATIENT, 2.6),
                ("Date of birth", base.dmy(PATIENT_DOB), 1.2),
                ("Gender", PATIENT_GENDER, 0.8),
            ],
        )
        y = base._box_row(
            pdf, y, [("Policy number", POLICY_NUMBER, 2), ("Member / insured ID", MEMBER_ID, 2)]
        )
        y = base._box_row(
            pdf,
            y,
            [
                ("Hospital", HOSPITAL, 2.6),
                ("City", HOSPITAL_CITY, 1.2),
                ("ROHINI ID", HOSPITAL_ROHINI, 1.4),
            ],
        )
        y = base._box_row(
            pdf,
            y,
            [("Treating doctor", SURGEON, 2.2), ("Registration no.", SURGEON_REGISTRATION, 1.6)],
        )
        y = base._band(pdf, y - 8, "CLINICAL DETAILS (TO BE FILLED BY THE TREATING DOCTOR)")
        y = base._labelled(pdf, y, "Presenting complaints", COMPLAINTS)
        y = base._labelled(
            pdf, y, "Provisional diagnosis", f"{DIAGNOSIS}    ICD-10: {HOSPITAL_ICD}"
        )
        y = base._labelled(pdf, y, "Proposed line of treatment (surgical)", PROCEDURE)
        y = base._box_row(
            pdf,
            y,
            [
                ("Date of admission", base.dmy(claim.admission), 1.3),
                ("Type of admission", "Emergency", 1.1),
                ("Expected stay (days)", str(EXPECTED_STAY), 1.1),
                ("Room type", "Semi-private", 1.1),
            ],
        )
        y = base._ticks(pdf, y - 6, "Injury or accident:", [("Yes", False), ("No", True)])
        y = base._band(pdf, y - 6, "ESTIMATED COST OF HOSPITALISATION")
        y = _heads_table(pdf, y, PREAUTH_ESTIMATE, "Total estimate")
        base._seal(pdf, base.RIGHT - 90, y - 40)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(base.LEFT + 4, y - 30, f"Signed: {SURGEON}, {base.dmy(claim.admission)}")
        base._footer(pdf, "Pre-auth Part C, page 1 of 1")
        pdf.showPage()

    return base._save(build)


def usg_report(claim: CashlessClaim) -> bytes:
    return _document(
        "RADIOLOGY REPORT - ULTRASOUND ABDOMEN AND PELVIS",
        claim,
        [
            ("Date", base.dmy(claim.admission)),
            (
                "Findings",
                "Blind-ending, non-compressible tubular structure in the right iliac fossa, "
                "9 mm in diameter, with periappendiceal fat stranding. No collection.",
            ),
            ("Impression", "Features suggestive of acute appendicitis."),
        ],
        "Ultrasound report, page 1 of 1",
    )


def enhancement_request(claim: CashlessClaim) -> bytes:
    def build(pdf: Canvas) -> None:
        y = _patient_rows(pdf, base._hospital_header(pdf, "REQUEST FOR ENHANCEMENT"), claim)
        y = base._labelled(pdf, y, "Date", base.dmy(claim.enhancement_day))
        y = base._labelled(pdf, y, "Reason for enhancement", ENHANCEMENT_REASON)
        y = base._box_row(
            pdf,
            y,
            [
                ("Extra days", "1", 1),
                ("Revised discharge", base.dmy(claim.discharge), 1.4),
                ("Extra amount (Rs.)", base.inr(_estimate_total(ENHANCEMENT_ESTIMATE)), 1.4),
            ],
        )
        y = base._band(pdf, y - 8, "EXTRA AMOUNT BY HEAD")
        y = _heads_table(pdf, y, ENHANCEMENT_ESTIMATE, "Extra amount")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(base.LEFT + 4, y - 20, f"Treating doctor: {SURGEON}")
        base._footer(pdf, "Enhancement request, page 1 of 1")
        pdf.showPage()

    return base._save(build)


def _bill(claim: CashlessClaim, title: str, number: str, items: list[Item], footer: str) -> bytes:
    def build(pdf: Canvas) -> None:
        y = base._hospital_header(pdf, title)
        y = base._box_row(
            pdf,
            y,
            [
                ("Bill No.", number, 2),
                ("IP No.", claim.ip_number, 1.4),
                ("Payer", "Cashless (TPA)", 1.4),
            ],
        )
        y = _patient_rows(pdf, y, claim)
        pdf.setFont("Helvetica-Bold", 8.5)
        pdf.drawString(base.LEFT + 4, y, "Date")
        pdf.drawString(base.LEFT + 78, y, "Particulars")
        pdf.drawRightString(372, y, "Qty")
        pdf.drawRightString(470, y, "Rate (Rs.)")
        pdf.drawRightString(base.RIGHT - 4, y, "Amount (Rs.)")
        y -= 16
        for section in SECTION_ORDER:
            rows = [i for i in items if i.section == section]
            if not rows:
                continue
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(base.LEFT + 4, y, section)
            y -= 13
            pdf.setFont("Helvetica", 8.6)
            for item in rows:
                pdf.drawString(base.LEFT + 4, y, base.dmon(claim.on(item.day)))
                pdf.drawString(base.LEFT + 78, y, item.description)
                pdf.drawRightString(372, y, f"{item.qty:g}")
                pdf.drawRightString(470, y, base.inr(item.rate))
                pdf.drawRightString(base.RIGHT - 4, y, base.inr(item.amount))
                y -= 12
            total = base.money(sum((i.amount for i in rows), ZERO))
            pdf.setFont("Helvetica-Bold", 8.6)
            pdf.drawRightString(470, y, "Section total")
            pdf.drawRightString(base.RIGHT - 4, y, base.inr(total))
            y -= 16
        grand = base.money(sum((i.amount for i in items), ZERO))
        pdf.setStrokeColor(black)
        pdf.line(base.LEFT, y + 6, base.RIGHT, y + 6)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(470, y - 8, "GRAND TOTAL")
        pdf.drawRightString(base.RIGHT - 4, y - 8, base.inr(grand))
        base._seal(pdf, base.RIGHT - 90, y - 70)
        base._footer(pdf, footer)
        pdf.showPage()

    return base._save(build)


def interim_bill(claim: CashlessClaim) -> bytes:
    """The running bill to day 2, before the enhancement."""
    to_date = [i for i in claim.items if i.day <= 2 and i.section != AMBULANCE]
    return _bill(
        claim,
        "INTERIM BILL (RUNNING)",
        f"{claim.bill_number}/INT",
        to_date,
        "Interim bill, page 1 of 1",
    )


def final_bill(claim: CashlessClaim) -> bytes:
    return _bill(
        claim,
        "FINAL BILL - INPATIENT (DETAILED)",
        claim.bill_number,
        claim.items,
        "Final bill, page 1 of 1",
    )


def discharge_summary(claim: CashlessClaim) -> bytes:
    return _document(
        "DISCHARGE SUMMARY",
        claim,
        [
            (
                "Dates",
                f"Admitted {base.dmy(claim.admission)}, discharged {base.dmy(claim.discharge)}",
            ),
            ("Final diagnosis", f"{FINAL_DIAGNOSIS}    ICD-10: {HOSPITAL_ICD}"),
            ("Procedure", f"{PROCEDURE} on {base.dmy(claim.on(1))} under general anaesthesia"),
            ("History", f"{COMPLAINTS}. No previous abdominal surgery."),
            (
                "Course in hospital",
                "Underwent laparoscopic appendicectomy on day 1. Developed paralytic ileus on "
                "day 2, managed with nil by mouth, IV fluids and nasogastric decompression; "
                "bowel sounds returned on day 3. Histopathology: acute suppurative appendicitis.",
            ),
            (
                "Advice on discharge",
                "Tab. Cefuroxime 500 mg twice daily for 5 days. Review in 7 days.",
            ),
        ],
        "Discharge summary, page 1 of 1",
    )


def ot_notes(claim: CashlessClaim) -> bytes:
    return _document(
        "OPERATION THEATRE NOTES",
        claim,
        [
            ("Date of surgery", base.dmy(claim.on(1))),
            ("Pre-operative diagnosis", DIAGNOSIS),
            ("Procedure", PROCEDURE),
            ("Operative findings", "Acutely inflamed appendix, 8 cm, no perforation or abscess."),
            ("Specimen", "Appendix sent for histopathology."),
        ],
        "OT notes, page 1 of 1",
    )


def histopathology_report(claim: CashlessClaim) -> bytes:
    return _document(
        "HISTOPATHOLOGY REPORT",
        claim,
        [
            ("Specimen", "Appendix, received in formalin"),
            ("Microscopy", "Transmural neutrophilic infiltrate with mucosal ulceration."),
            ("Impression", "Acute suppurative appendicitis."),
        ],
        "Histopathology report, page 1 of 1",
    )


# --- Bundle ----------------------------------------------------------------------------------

UPLOADS: dict[str, list[tuple[str, DocType, Any]]] = {
    "preauth": [
        ("01_preauth_form_part_c.pdf", DocType.PREAUTH_FORM, preauth_form),
        ("02_usg_report.pdf", DocType.INVESTIGATION_REPORT, usg_report),
    ],
    "enhancement": [
        ("03_enhancement_request.pdf", DocType.ENHANCEMENT_REQUEST, enhancement_request),
        ("04_interim_bill.pdf", DocType.INTERIM_BILL, interim_bill),
    ],
    "discharge": [
        ("05_discharge_summary.pdf", DocType.DISCHARGE_SUMMARY, discharge_summary),
        ("06_ot_notes.pdf", DocType.OT_NOTES, ot_notes),
        ("07_histopathology_report.pdf", DocType.INVESTIGATION_REPORT, histopathology_report),
        ("08_final_bill.pdf", DocType.FINAL_BILL, final_bill),
    ],
}
GROUND_TRUTH = "ground_truth.json"


def generate(out_dir: Path, admission: date) -> dict[str, Any]:
    """Write the three uploads and the ground truth. Returns the ground truth."""
    out_dir.mkdir(parents=True, exist_ok=True)
    claim = CashlessClaim(admission=admission)
    files: dict[str, Any] = {}
    for request, uploads in UPLOADS.items():
        files[request] = []
        for name, doc_type, render in uploads:
            data = render(claim)
            (out_dir / name).write_bytes(data)
            files[request].append(
                {"file": name, "doc_type": doc_type.value, "pages": base.page_count(data)}
            )
    rules = DefaultRulesEngine(REPO / "config" / "products")
    truth: dict[str, Any] = ground_truth(claim, rules, files)
    (out_dir / GROUND_TRUTH).write_text(json.dumps(truth, indent=2) + "\n")
    return truth


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--admission-date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=STAY_DAYS),
        help="admission date, YYYY-MM-DD (default: the stay ends today)",
    )
    args = parser.parse_args()
    truth = generate(args.out, args.admission_date)
    print(f"Wrote the cashless admission to {args.out}")
    for request, expected in truth["requests"].items():
        sheet = expected["sheet"]
        print(
            f"  {request:<11} claimed Rs {sheet['claimed_total']:>10}  admissible Rs "
            f"{sheet['admissible_total']:>10}  payable Rs {sheet['payable']:>10}  "
            f"approved total Rs {expected['approved_total']:>10}  rules failed "
            f"{','.join(sheet['rules_failed'])}"
        )
    print(
        f"  R26 variance Rs {truth['expected']['r26_variance']}; hospital paid Rs "
        f"{truth['expected']['amount_paid']}"
    )


if __name__ == "__main__":
    main()
