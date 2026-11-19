"""Generate eval cases from case specs: one bundle of PDFs per case plus its ground truth.

    uv run --project backend python tools/mockgen/corpus.py --out .local/eval/corpus

A CaseSpec names a category, a seed, the mock_core member and seeded hospital, a treatment,
the claim route, the seeded problems and the rendering variant. generate_case() turns it into
documents and a ground_truth.json that records every page label, every extracted field the
eval scores, every bill line, the deduction sheet with rule IDs, the expected codes and
package, the seeded problems and the payable.

Rules:
- Every amount in the ground truth comes from the rules engine (tools/mockgen/sheet.py),
  never typed by hand.
- Deterministic: the same spec and the same `today` give the same bytes. Randomness comes only
  from random.Random(spec.seed); noise images are built from that generator, not from PIL's
  unseeded noise.
- People and hospitals are read from the seeds (backend/app/mock_core/seed.py,
  backend/app/database/seed.py), so a case always matches the data the stack holds.
- All data is synthetic.

CORPUS holds the ten approved categories, one claim each.
"""

import argparse
import hashlib
import io
import json
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parents[2]
for _path in (REPO, REPO / "backend"):  # run as a script: import the generators and the app
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pypdfium2 as pdfium  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402
from reportlab.lib.colors import Color, black, white  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen.canvas import Canvas  # noqa: E402

from app.database.seed import _HOSPITALS, AGREEMENTS  # noqa: E402
from app.mock_core.seed import _POLICIES, _member_id, _policy_number  # noqa: E402
from app.services.digitisation._helpers import BANK_CODES  # noqa: E402
from app.services.rules.contract import (  # noqa: E402
    ClaimRoute,
    Head,
    MappedPackage,
    PreAuthFacts,
)
from app.services.rules.service import DefaultRulesEngine  # noqa: E402
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS  # noqa: E402
from app.shared.types import DocType  # noqa: E402
from tools.mockgen import tracer_claim as base  # noqa: E402
from tools.mockgen.sheet import SheetLine, SheetTerms, expected_sheet, money  # noqa: E402

D = Decimal
ZERO = D("0.00")
ONE = D("1")
GROUND_TRUTH = "ground_truth.json"
MERGED = "bundle_merged.pdf"
INITIAL_WAIT_DAYS = 30  # R5, the product's initial waiting period


class Problem(StrEnum):
    MISSING_DOCUMENT = "missing_document"
    HIDDEN_NON_PAYABLE = "hidden_non_payable"
    UNBUNDLING = "unbundling"
    WAITING_PERIOD = "waiting_period"
    LONG_STAY = "long_stay"
    UNDECLARED_PED = "undeclared_ped"
    DUPLICATE_FILE = "duplicate_file"
    BANK_MISMATCH = "bank_mismatch"
    INFLATED_BILL = "inflated_bill"
    LAPSED_POLICY = "lapsed_policy"


Request = Literal["preauth", "enhancement", "discharge"]


@dataclass(frozen=True)
class Variant:
    """How the documents look and travel. Doc types name the documents a variant applies to."""

    delivery: Literal["merged", "slots"] = "merged"
    scans: frozenset[DocType] = frozenset()  # flatbed scan: slight skew, speckle, no text
    photo: frozenset[DocType] = frozenset()  # phone photo: perspective, shading, blur
    stamps: frozenset[DocType] = frozenset()  # a red "RECEIVED" rubber stamp
    handwritten_form: bool = False  # the member fills Part A by hand (oblique blue ink)
    plain_bill: bool = False  # the final bill is not on the hospital's letterhead (RF9)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    category: str
    seed: int
    route: ClaimRoute
    policy: int  # mock_core policy n (1 to 6)
    member: int  # member m on that policy (1 = the proposer)
    hospital: int  # index into the seeded hospitals
    treatment: str  # key of TREATMENTS
    room: Literal["general", "semi_private", "deluxe"]
    problems: tuple[Problem, ...]
    variant: Variant
    expect: dict[str, Any]  # the end state the category should reach, e.g. final_status
    missing: DocType = DocType.PAYMENT_RECEIPT  # the document MISSING_DOCUMENT leaves out
    reuse_from: str | None = None  # DUPLICATE_FILE: the case whose file is reused
    reuse_doc: DocType = DocType.DISCHARGE_SUMMARY
    requests: tuple[Request, ...] = ("preauth", "enhancement", "discharge")  # cashless only
    extra_days: int = 0  # cashless: days past the pre-auth's expected stay (an enhancement)

    def validate(self) -> None:
        persona(self)  # raises on an unknown policy or member
        if self.treatment not in TREATMENTS:
            raise ValueError(f"unknown treatment {self.treatment!r}")
        if not 0 <= self.hospital < len(HOSPITAL_ROHINI_IDS):
            raise ValueError(f"unknown hospital index {self.hospital}")
        if Problem.UNBUNDLING in self.problems and self.route != "cashless":
            raise ValueError("unbundling (R23) needs the cashless route: packages bind cashless")
        if Problem.WAITING_PERIOD in self.problems:
            start_ago = next(p[4] for p in _POLICIES if p[0] == self.policy)
            if start_ago >= INITIAL_WAIT_DAYS:
                raise ValueError(
                    f"waiting_period needs a policy inside its initial wait; policy "
                    f"{self.policy} started {start_ago} days before the seed day"
                )
        if Problem.LAPSED_POLICY in self.problems:
            start_ago = next(p[4] for p in _POLICIES if p[0] == self.policy)
            if start_ago < 365 + INITIAL_WAIT_DAYS:
                raise ValueError(
                    f"lapsed_policy needs a policy past its grace period; policy "
                    f"{self.policy} started {start_ago} days before the seed day"
                )
        if Problem.DUPLICATE_FILE in self.problems and self.reuse_from is None:
            raise ValueError("duplicate_file needs reuse_from")
        if self.route == "reimbursement" and self.requests != CaseSpec.requests:
            raise ValueError("requests apply to the cashless route only")


# --- People, hospitals and treatments (from the seeds) -------------------------------------


@dataclass(frozen=True)
class Persona:
    policy_number: str
    member_id: str
    name: str
    dob: date
    gender: str  # "Female" or "Male"
    relationship: str
    sum_insured: Decimal
    inception_ago: int  # days before the seed day
    start_ago: int
    declared_ped_codes: tuple[str, ...]


def persona(spec: CaseSpec) -> Persona:
    rows = [p for p in _POLICIES if p[0] == spec.policy]
    if not rows:
        raise ValueError(f"no mock_core policy {spec.policy}")
    n, si, _bonus, inception_ago, start_ago, _premium, peds, people = rows[0]
    if not 1 <= spec.member <= len(people):
        raise ValueError(f"policy {spec.policy} has no member {spec.member}")
    name, dob, gender, relationship = people[spec.member - 1]
    return Persona(
        policy_number=_policy_number(n),
        member_id=_member_id(n, spec.member),
        name=name,
        dob=dob,
        gender="Female" if gender == "F" else "Male",
        relationship=relationship,
        sum_insured=D(si),
        inception_ago=inception_ago,
        start_ago=start_ago,
        declared_ped_codes=tuple(p["code"] for p in peds),
    )


@dataclass(frozen=True)
class HospitalProfile:
    rohini: str
    name: str
    city: str
    state: str
    pincode: str
    differential_billing: bool
    scheme: str
    discount: Decimal | None


def hospital(index: int) -> HospitalProfile:
    name, city, state, pincode, _beds, differential = _HOSPITALS[index]
    agreement = AGREEMENTS[index]
    return HospitalProfile(
        rohini=HOSPITAL_ROHINI_IDS[index],
        name=name,
        city=city,
        state=state,
        pincode=pincode,
        differential_billing=differential,
        scheme=agreement.scheme,
        discount=agreement.non_package_discount_percent,
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
        return money(self.qty * self.rate)


ROOM_RATES = {"general": D("2000"), "semi_private": D("3500"), "deluxe": D("7500")}
ROOM_NAMES = {
    "general": "General Ward Bed",
    "semi_private": "Semi-private Room Rent",
    "deluxe": "Deluxe Single Room Rent",
}
SECTIONS = [
    "ROOM CHARGES",
    "PACKAGE",
    "OT CHARGES",
    "PROFESSIONAL FEES",
    "PHARMACY",
    "CONSUMABLES",
    "INVESTIGATIONS",
]


@dataclass(frozen=True)
class Treatment:
    diagnosis: str
    icd_written: str  # what the hospital writes (often less specific than the verified code)
    icd_code: str  # the billable ICD-10-CM code medical review should verify
    icd_family: str  # its 3-character category
    procedure: str | None
    pcs_code: str | None
    pcs_family: str | None
    package: MappedPackage | None  # under a CGHS agreement; None when nothing maps
    guideline_code: str | None
    los_norm_days: int
    stay_days: int
    complaints: str
    course: str
    items: Callable[[int, str], list[Item]]  # (stay days, room key) -> bill items


def _appendicectomy_items(stay: int, room: str) -> list[Item]:
    one = ONE
    rate = ROOM_RATES[room]
    items = [Item("ROOM CHARGES", Head.ROOM, d, ROOM_NAMES[room], one, rate) for d in range(stay)]
    items += [
        Item(
            "OT CHARGES",
            Head.OT,
            1,
            "Operation Theatre Charges - Major (up to 2 hrs)",
            one,
            D("8000"),
        ),
        Item("OT CHARGES", Head.OT, 1, "Laparoscopy Equipment Charges", one, D("2500")),
        Item("PROFESSIONAL FEES", Head.PROFESSIONAL_FEES, 1, "Surgeon Fee", one, D("10000")),
        Item("PROFESSIONAL FEES", Head.PROFESSIONAL_FEES, 1, "Anaesthetist Fee", one, D("3000")),
    ]
    items += [
        Item("PROFESSIONAL FEES", Head.PROFESSIONAL_FEES, d, "Consultant Visit", one, D("800"))
        for d in range(2, stay)
    ]
    # Pharmacy at or below the NPPA ceilings, so a clean case has no drug-price finding.
    items += [
        Item("PHARMACY", Head.PHARMACY, 0, "Inj. Ceftriaxone 1 g", D("6"), D("60.00")),
        Item("PHARMACY", Head.PHARMACY, 0, "Inj. Pantoprazole 40 mg", D("5"), D("48.00")),
        Item("PHARMACY", Head.PHARMACY, 0, "IV Fluid Ringer Lactate 500 ml", D("8"), D("46.00")),
        Item(
            "PHARMACY",
            Head.PHARMACY,
            stay,
            "Tab. Cefuroxime 500 mg (discharge)",
            D("10"),
            D("42.80"),
        ),
        Item(
            "CONSUMABLES",
            Head.CONSUMABLES,
            1,
            "Laparoscopic Trocar Kit (disposable)",
            one,
            D("3800"),
        ),
        Item("CONSUMABLES", Head.CONSUMABLES, 1, "Absorbable Suture Vicryl 2-0", D("2"), D("610")),
        Item("INVESTIGATIONS", Head.DIAGNOSTICS, 0, "Complete Blood Count (CBC)", one, D("420")),
        Item(
            "INVESTIGATIONS", Head.DIAGNOSTICS, 0, "Ultrasound Abdomen and Pelvis", one, D("1500")
        ),
        Item(
            "INVESTIGATIONS",
            Head.DIAGNOSTICS,
            2,
            "Histopathology - Appendix Specimen",
            one,
            D("1200"),
        ),
    ]
    return items


TREATMENTS: dict[str, Treatment] = {
    "lap_appendicectomy": Treatment(
        diagnosis="Acute appendicitis",
        icd_written="K35.8",
        icd_code="K35.80",  # acute appendicitis without perforation, gangrene or abscess
        icd_family="K35",
        procedure="Laparoscopic appendicectomy",
        pcs_code=base.PCS_CODE,
        pcs_family="0DTJ",
        package=base.PACKAGE,  # CGHS AG095; tests/pg checks the rate against the loaded list
        guideline_code=base.GUIDELINE_CODE,
        los_norm_days=base.LOS_NORM_DAYS,
        stay_days=3,
        complaints="Pain in the right lower abdomen since 2 days, nausea and low grade fever.",
        course=(
            "Laparoscopic appendicectomy on day 1 under general anaesthesia. Uneventful "
            "recovery; oral diet from post-operative day 1. Histopathology: acute suppurative "
            "appendicitis."
        ),
        items=_appendicectomy_items,
    ),
}

# One treating surgeon per seeded hospital, so RF5 (one doctor, one diagnosis, many claims)
# does not fire across hospitals.
SURGEONS = [
    "Dr. Rajiv Khanna",
    "Dr. Anil Bhatia",
    "Dr. Shalini Srivastava",
    "Dr. Prakash Kulkarni",
    "Dr. Kavya Reddy",
]

# Non-payable list I items a hidden line may carry. Each is in the seeded fixture and in the
# IRDAI list; generate_case() asserts R11 deducts the chosen one.
HIDDEN_ITEMS = [
    ("Gloves", D("12"), D("22.00")),
    ("Mask", D("10"), D("15.00")),
    ("Thermometer", ONE, D("180.00")),
]
UNDECLARED_PED = ("I10", "Known case of hypertension for 6 years, on Tab. Amlodipine 5 mg daily", 6)


# --- The case: a spec made concrete for one day ---------------------------------------------


@dataclass
class Case:
    spec: CaseSpec
    today: date
    rng: random.Random
    person: Persona
    hosp: HospitalProfile
    treatment: Treatment
    admission: date
    stay: int
    items: list[Item]
    serial: str
    account: str
    ifsc: str
    cheque_account: str
    problems: list[dict[str, Any]] = field(default_factory=list)
    hidden_description: str | None = None

    @property
    def discharge(self) -> date:
        return self.admission + timedelta(days=self.stay)

    def on(self, day: int) -> date:
        return self.admission + timedelta(days=day)

    @property
    def age(self) -> int:
        a, b = self.admission, self.person.dob
        return a.year - b.year - ((a.month, a.day) < (b.month, b.day))

    @property
    def bill_number(self) -> str:
        prefix = "".join(w[0] for w in self.hosp.name.split()[:3]).upper()
        return f"{prefix}/IP/{self.admission:%y%m}/{self.serial}"

    @property
    def ip_number(self) -> str:
        return f"IP{self.admission:%y}-{self.serial}"

    @property
    def ordered_items(self) -> list[Item]:
        return [
            i
            for s in SECTIONS
            for i in sorted((x for x in self.items if x.section == s), key=lambda x: x.day)
        ]

    @property
    def grand_total(self) -> Decimal:
        return money(sum((i.amount for i in self.items), ZERO))

    @property
    def surgeon(self) -> str:
        return SURGEONS[self.spec.hospital]

    @property
    def title(self) -> str:
        return "Mrs." if self.person.gender == "Female" else "Mr."


def _case(spec: CaseSpec, today: date) -> Case:
    spec.validate()
    rng = random.Random(spec.seed)
    person = persona(spec)
    treatment = TREATMENTS[spec.treatment]
    stay = treatment.stay_days + (spec.extra_days if spec.route == "cashless" else 0)
    if Problem.LONG_STAY in spec.problems:
        stay = max(stay, treatment.los_norm_days + 2)  # RF12 fires from norm + 2
    if spec.route == "cashless":
        admission = today - timedelta(days=stay)  # a live admission: discharged today
    elif Problem.WAITING_PERIOD in spec.problems:
        # Inside the initial wait: after the policy began, discharged by today.
        admission = today - timedelta(days=min(person.start_ago - 2, stay + 2))
    else:
        # Weeks back by seed, so cases of one member do not overlap (RF6), inside the policy.
        weeks = 3 + spec.seed % 8
        admission = today - timedelta(days=7 * weeks + stay)
    if Problem.LAPSED_POLICY not in spec.problems:
        if admission <= today - timedelta(days=person.start_ago):
            raise ValueError(f"{spec.case_id}: admission falls before the policy start")
    items = treatment.items(stay, spec.room)
    if Problem.INFLATED_BILL in spec.problems:
        items = [
            Item(i.section, i.head, i.day, i.description, i.qty, money(i.rate * D("1.6")))
            if i.head in (Head.OT, Head.PROFESSIONAL_FEES, Head.CONSUMABLES)
            else i
            for i in items
        ]
    account = "".join(str(rng.randrange(10)) for _ in range(11))
    bank_code = rng.choice(["BSBL", "PUNB", "CNRB", "BARB"])
    case = Case(
        spec=spec,
        today=today,
        rng=rng,
        person=person,
        hosp=hospital(spec.hospital),
        treatment=treatment,
        admission=admission,
        stay=stay,
        items=items,
        serial=f"{rng.randrange(100000):05d}",
        account=account,
        ifsc=f"{bank_code}0{rng.randrange(1000000):06d}",
        cheque_account=account,
    )
    _seed_problems(case)
    return case


def _seed_problems(case: Case) -> None:
    spec = case.spec
    for problem in spec.problems:
        entry: dict[str, Any] = {"type": problem.value}
        if problem is Problem.MISSING_DOCUMENT:
            entry["doc_type"] = spec.missing.value
        elif problem is Problem.HIDDEN_NON_PAYABLE:
            name, qty, rate = case.rng.choice(HIDDEN_ITEMS)
            case.items.append(Item("CONSUMABLES", Head.CONSUMABLES, 1, name, qty, rate))
            case.hidden_description = name
        elif problem is Problem.UNBUNDLING:
            package = case.treatment.package
            if package is None:
                raise ValueError(f"{spec.case_id}: unbundling needs a treatment with a package")
            # The package is billed as one line, and the trocar kit, which it includes, is
            # charged again: R23 deducts it.
            case.items = [i for i in case.items if i.head not in (Head.OT, Head.PROFESSIONAL_FEES)]
            case.items.append(
                Item(
                    "PACKAGE",
                    Head.PACKAGE,
                    1,
                    f"{package.name} package ({package.scheme} {package.package_code})",
                    ONE,
                    package.rate,
                )
            )
            entry["description"] = "Laparoscopic Trocar Kit (disposable)"
        elif problem is Problem.BANK_MISMATCH:
            digits = list(case.account)
            digits[-3], digits[-2] = digits[-2], digits[-3]
            if digits == list(case.account):
                digits[-1] = str((int(digits[-1]) + 1) % 10)
            case.cheque_account = "".join(digits)
            entry.update(form_account=case.account, cheque_account=case.cheque_account)
        elif problem is Problem.UNDECLARED_PED:
            code, text, years = UNDECLARED_PED
            entry.update(
                icd_family=code,
                history_text=text,
                onset_before=(case.admission - timedelta(days=365 * years)).isoformat(),
            )
        elif problem is Problem.LONG_STAY:
            entry.update(stay_days=case.stay, los_norm_days=case.treatment.los_norm_days)
        elif problem is Problem.WAITING_PERIOD:
            entry.update(rule_id="R5", policy_start_days_ago=case.person.start_ago)
        elif problem is Problem.DUPLICATE_FILE:
            entry.update(reuse_from=spec.reuse_from, doc_type=spec.reuse_doc.value)
        elif problem is Problem.INFLATED_BILL:
            entry["factor"] = "1.6"
        elif problem is Problem.LAPSED_POLICY:
            entry.update(rule_id="R2", policy_start_days_ago=case.person.start_ago)
        case.problems.append(entry)


# --- Ground truth ---------------------------------------------------------------------------


def _expected_package(case: Case) -> str | None:
    """The package bill review should map to under the hospital's agreement (our crosswalk:
    CGHS AG095, PM-JAY SG017B for a laparoscopic appendicectomy); none on its own tariff."""
    package = case.treatment.package
    if package is None or case.hosp.scheme == "OWN":
        return None
    if case.hosp.scheme == "PMJAY":
        return case.treatment.guideline_code
    return package.package_code


def sheet_terms(spec: CaseSpec) -> SheetTerms:
    treatment = TREATMENTS[spec.treatment]
    hosp = hospital(spec.hospital)
    priced = hosp.scheme == "CGHS" and treatment.package is not None
    return SheetTerms(
        route=spec.route,
        sum_insured=persona(spec).sum_insured,
        differential_billing=hosp.differential_billing,
        packages=[treatment.package] if priced and treatment.package else [],
        non_package_discount=hosp.discount,
        procedure_codes=[treatment.pcs_code] if treatment.pcs_code else [],
        billed_as_package=Problem.UNBUNDLING in spec.problems,
    )


def _lines(case: Case) -> list[SheetLine]:
    return [
        SheetLine(n, i.head, i.description, i.qty, i.rate, i.amount, case.on(i.day))
        for n, i in enumerate(case.ordered_items, start=1)
    ]


def _estimate(case: Case, days: int, start: int = 0) -> list[SheetLine]:
    """A cashless estimate by head for the days [start, days): the planned bill's heads,
    rounded up to Rs 100, room days as qty."""
    by_head: dict[Head, Decimal] = {}
    room_days = 0
    for item in case.items:
        if start <= min(item.day, case.stay - 1) < days:
            by_head[item.head] = by_head.get(item.head, ZERO) + item.amount
            room_days += 1 if item.head is Head.ROOM else 0
    lines = []
    for n, (head, amount) in enumerate(sorted(by_head.items(), key=lambda kv: kv[0].value), 1):
        rounded = money(-(-amount // 100) * 100)
        qty = D(room_days) if head is Head.ROOM and room_days else None
        rate = rounded / qty if qty else None
        lines.append(
            SheetLine(n, head, head.value.replace("_", " ").title(), qty, rate, rounded, None)
        )
    return lines


def _expected_decision(case: Case, sheet: dict[str, Any]) -> str:
    stated = case.spec.expect.get("decision")
    if isinstance(stated, str):
        return stated
    return "partial" if sheet["deductions"] else "approve"


def _fields(case: Case) -> dict[str, Any]:
    p, h = case.person, case.hosp
    fields: dict[str, Any] = {
        "identity.patient_name": p.name,
        "identity.policy_number": p.policy_number,
        "identity.member_id": p.member_id,
        "hospital.name": h.name,
        "hospital.city": h.city,
        "admission.admission_date": case.admission,
    }
    if case.spec.route == "reimbursement":  # Part A prints them; Part C only age and sex
        fields |= {"identity.date_of_birth": p.dob, "identity.gender": p.gender}
    if "discharge" in _requests(case):
        fields |= {
            "clinical.patient_name": p.name,
            "clinical.admission_date": case.admission,
            "clinical.discharge_date": case.discharge,
        }
    if case.spec.route == "reimbursement":
        fields |= {
            "admission.discharge_date": case.discharge,
            "admission.accident": False,
            "claimed.hospital_expenses": case.grand_total,
            "claimed.total": case.grand_total,
            "claim_form_bank.account_number": case.account,
            "claim_form_bank.ifsc": case.ifsc,
            "claim_form_bank.account_holder": p.name.upper(),
            "bank.account_number": case.cheque_account,
            "bank.ifsc": case.ifsc,
            "bank.account_holder": p.name.upper(),
        }
    else:
        fields |= {
            "preauth.rohini_id": h.rohini,
            "preauth.treating_doctor": case.surgeon,
            "preauth.provisional_diagnosis": case.treatment.diagnosis,
            "preauth.proposed_treatment": case.treatment.procedure,
            "preauth.icd_code_written": case.treatment.icd_written,
            "preauth.expected_stay_days": case.treatment.stay_days,
        }
    if "discharge" in _requests(case):
        fields |= {
            "bill.bill_number": case.bill_number,
            "bill.patient_name": p.name,
            "bill.grand_total": case.grand_total,
        }
    return fields


def _requests(case: Case) -> tuple[str, ...]:
    return case.spec.requests if case.spec.route == "cashless" else ("discharge",)


def ground_truth(case: Case, rules: DefaultRulesEngine, files: dict[str, Any]) -> dict[str, Any]:
    spec, t = case.spec, case.treatment
    terms = sheet_terms(spec)
    requests: dict[str, Any] = {}
    sheet: dict[str, Any]
    if spec.route == "reimbursement":
        sheet = expected_sheet(
            _lines(case), rules, terms, admission=case.admission, discharge=case.discharge
        )
    else:
        planned = t.stay_days
        approved = approved_admissible = ZERO
        phases: dict[str, dict[str, Any]] = {}
        if "preauth" in spec.requests:
            pre = expected_sheet(
                _estimate(case, planned), rules, terms, admission=case.admission, discharge=None
            )
            phases["preauth"] = {"sheet": pre, "approved_total": pre["payable"]}
            approved, approved_admissible = pre["payable"], pre["admissible_total"]
        if "enhancement" in spec.requests:
            heads = _estimate(case, planned) + [
                SheetLine(
                    100 + ln.line_id, ln.head, ln.description, ln.qty, ln.rate, ln.claimed, None
                )
                for ln in _estimate(case, case.stay, start=planned)
            ]
            enh = expected_sheet(heads, rules, terms, admission=case.admission, discharge=None)
            approved = max(approved, enh["payable"])
            approved_admissible = max(approved_admissible, enh["admissible_total"])
            phases["enhancement"] = {"sheet": enh, "approved_total": approved}
        if "discharge" in spec.requests:
            sheet = expected_sheet(
                _lines(case),
                rules,
                terms,
                admission=case.admission,
                discharge=case.discharge,
                preauth=PreAuthFacts(approved_total=approved_admissible),
            )
            phases["discharge"] = {"sheet": sheet, "approved_total": sheet["payable"]}
        sheet = phases[spec.requests[-1]]["sheet"]
        requests = phases
    if case.hidden_description is not None:
        [hidden] = [ln for ln in sheet["lines"] if ln["description"] == case.hidden_description]
        if hidden["rule_id"] != "R11":
            raise AssertionError(f"R11 does not deduct the hidden item {case.hidden_description}")
        next(p for p in case.problems if p["type"] == "hidden_non_payable").update(
            line_id=hidden["line_id"], description=hidden["description"]
        )
    for entry in case.problems:
        if entry["type"] == "unbundling":
            [line] = [ln for ln in sheet["lines"] if ln["description"] == entry["description"]]
            if line["rule_id"] != "R23":
                raise AssertionError("R23 does not deduct the unbundled line")
            entry["line_id"] = line["line_id"]
    decision = _expected_decision(case, sheet)
    paid = ZERO if decision == "reject" or "discharge" not in _requests(case) else sheet["payable"]
    form = _form(case)
    truth = {
        "about": f"Eval case {spec.case_id} ({spec.category}). All data is synthetic.",
        "case_id": spec.case_id,
        "category": spec.category,
        "seed": spec.seed,
        "route": spec.route,
        "admission_date": case.admission,
        "discharge_date": case.discharge,
        "persona": {
            "policy_number": case.person.policy_number,
            "member_id": case.person.member_id,
            "sum_insured": case.person.sum_insured,
            "inception_date": case.today - timedelta(days=case.person.inception_ago),
            "policy_start": case.today - timedelta(days=case.person.start_ago),
            "declared_ped_codes": list(case.person.declared_ped_codes),
        },
        "hospital": {
            "rohini": case.hosp.rohini,
            "name": case.hosp.name,
            "scheme": case.hosp.scheme,
        },
        "submitter": "member" if spec.route == "reimbursement" else "hospital",
        "form": form,
        "files": files,
        "fields": _fields(case),
        "bill": {
            "bill_number": case.bill_number,
            "lines": [
                {
                    "line_id": n,
                    "section": i.section,
                    "head": i.head.value,
                    "date": case.on(i.day),
                    "description": i.description,
                    "qty": i.qty,
                    "rate": i.rate,
                    "amount": i.amount,
                }
                for n, i in enumerate(case.ordered_items, start=1)
            ],
            "grand_total": case.grand_total,
        },
        "sheet": sheet,
        "requests": requests,
        "problems": case.problems,
        "expected": {
            **spec.expect,
            "decision": decision,
            "icd_code": t.icd_code,
            "icd_family": t.icd_family,
            "pcs_code": t.pcs_code,
            "pcs_family": t.pcs_family,
            "package": _expected_package(case),
            "package_rate": t.package.rate
            if t.package
            and case.hosp.scheme == "CGHS"
            and AGREEMENTS[spec.hospital].accreditation == "nabh"
            else None,
            "guideline_code": t.guideline_code,
            "los_norm_days": t.los_norm_days,
            "amount_paid": paid,
        },
    }
    result: dict[str, Any] = base._jsonable(truth)
    return result


def _form(case: Case) -> dict[str, Any]:
    p, h = case.person, case.hosp
    if case.spec.route == "cashless":
        estimate = sum((ln.claimed for ln in _estimate(case, case.treatment.stay_days)), ZERO)
        return {
            "policy_number": p.policy_number,
            "member_id": p.member_id,
            "patient_name": p.name,
            "hospital_id": h.rohini,
            "hospital_name": h.name,
            "admission_date": case.admission,
            "claimed_amount": money(estimate),
            "request_type": "preauth",
        }
    return {
        "policy_number": p.policy_number,
        "member_id": p.member_id,
        "patient_name": p.name,
        "hospital_name": h.name,
        "hospital_city": h.city,
        "admission_date": case.admission,
        "discharge_date": case.discharge,
        "claimed_amount": case.grand_total,
        "claim_category": "inpatient",
        "cause": "illness",
        "bank_account_number": case.account,
        "ifsc": case.ifsc,
        "declaration_accepted": True,
    }


# --- Documents ------------------------------------------------------------------------------

W, H, LEFT, RIGHT = base.W, base.H, base.LEFT, base.RIGHT
INK = Color(0.1, 0.18, 0.55)


def _header(pdf: Canvas, case: Case, title: str) -> float:
    h = case.hosp
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawCentredString(W / 2, H - 48, h.name.upper())
    pdf.setFont("Helvetica", 8.5)
    pdf.drawCentredString(W / 2, H - 61, f"{h.city}, {h.state} {h.pincode}")
    pdf.drawCentredString(W / 2, H - 72, f"ROHINI ID {h.rohini}")
    pdf.setLineWidth(1.2)
    pdf.line(LEFT, H - 80, RIGHT, H - 80)
    pdf.setLineWidth(1)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(W / 2, H - 98, title)
    return H - 110


def _seal(pdf: Canvas, case: Case, x: float, y: float) -> None:
    pdf.saveState()
    pdf.setStrokeColor(INK)
    pdf.setFillColor(INK)
    pdf.circle(x, y, 44, stroke=1, fill=0)
    pdf.circle(x, y, 39, stroke=1, fill=0)
    pdf.setFont("Helvetica-Bold", 5.5)
    words = case.hosp.name.upper().split()
    pdf.drawCentredString(x, y + 8, " ".join(words[:2]))
    pdf.drawCentredString(x, y, " ".join(words[2:]) or "HOSPITAL")
    pdf.drawCentredString(x, y - 8, case.hosp.city.upper())
    pdf.restoreState()


def _stamp(pdf: Canvas, case: Case) -> None:
    """A red rubber stamp across the page's upper right, slightly rotated."""
    pdf.saveState()
    pdf.setStrokeColor(Color(0.78, 0.1, 0.1))
    pdf.setFillColor(Color(0.78, 0.1, 0.1))
    pdf.translate(RIGHT - 110, H - 160)
    pdf.rotate(case.rng.uniform(-14, -6))
    pdf.setLineWidth(2)
    pdf.roundRect(-70, -22, 140, 44, 6, stroke=1, fill=0)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(0, 2, "RECEIVED")
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(0, -14, f"BILLING DESK  {base.dmy(case.discharge)}")
    pdf.restoreState()


def _patient_row(pdf: Canvas, case: Case, y: float) -> float:
    sex = "F" if case.person.gender == "Female" else "M"
    return base._box_row(
        pdf,
        y,
        [
            ("Patient name", f"{case.title} {case.person.name.upper()}", 3),
            ("Age / Sex", f"{case.age} Y / {sex}", 1.3),
            ("IP No.", case.ip_number, 1.8),
        ],
    )


def _document(case: Case, title: str, sections: list[tuple[str, str]], footer: str) -> bytes:
    def build(pdf: Canvas) -> None:
        y = _patient_row(pdf, case, _header(pdf, case, title)) - 12
        for label, text in sections:
            y = base._labelled(pdf, y, label.upper(), text)
        pdf.setFont("Helvetica-Oblique", 12)
        pdf.drawString(LEFT + 10, y - 30, case.surgeon.replace("Dr. ", ""))
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(LEFT + 10, y - 44, case.surgeon)
        _seal(pdf, case, RIGHT - 90, y - 50)
        base._footer(pdf, footer)
        pdf.showPage()

    return base._save(build)


def _value(pdf: Canvas, case: Case, x: float, y: float, text: str) -> None:
    """A filled-in value: typed (Courier) or, on a handwritten form, oblique blue ink with
    a small seeded wobble per character."""
    if not case.spec.variant.handwritten_form:
        pdf.setFont("Courier-Bold", 9.5)
        pdf.drawString(x, y, text)
        return
    pdf.saveState()
    pdf.setFillColor(INK)
    for char in text:
        size = 10.5 + case.rng.uniform(-0.8, 0.8)
        pdf.setFont("Helvetica-Oblique", size)
        pdf.drawString(x, y + case.rng.uniform(-1.2, 1.2), char)
        x += pdf.stringWidth(char, "Helvetica-Oblique", size) + case.rng.uniform(-0.2, 0.6)
    pdf.restoreState()


def _form_row(pdf: Canvas, case: Case, y: float, cells: list[tuple[str, str, float]]) -> float:
    x, total, height = LEFT, sum(s for *_, s in cells), 26
    for label, value, share in cells:
        width = (RIGHT - LEFT) * share / total
        pdf.rect(x, y - height, width, height, stroke=1, fill=0)
        pdf.setFont("Helvetica", 6.8)
        pdf.setFillColor(base.GREY)
        pdf.drawString(x + 3, y - 8.5, label)
        pdf.setFillColor(black)
        _value(pdf, case, x + 3, y - height + 6, value)
        x += width
    return y - height


def claim_form_part_a(case: Case) -> bytes:
    p, h = case.person, case.hosp

    def build(pdf: Canvas) -> None:
        y = base._insurer_header(pdf, "PART A", "INSURED")
        y = base._band(pdf, y, "A. DETAILS OF PRIMARY INSURED")
        y = _form_row(
            pdf, case, y, [("a) Policy No.", p.policy_number, 3), ("c) Member ID", p.member_id, 3)]
        )
        y = (
            _form_row(
                pdf,
                case,
                y,
                [
                    ("d) Name", p.name.upper(), 3),
                    ("Date of birth", base.dmy(p.dob), 2),
                    ("Gender", p.gender, 1.4),
                ],
            )
            - 6
        )
        y = base._band(pdf, y, "D. DETAILS OF HOSPITALISATION")
        y = _form_row(pdf, case, y, [("Hospital", h.name, 3), ("City", h.city, 1.4)])
        y = (
            _form_row(
                pdf,
                case,
                y,
                [
                    ("Date of admission", base.dmy(case.admission), 2),
                    ("Date of discharge", base.dmy(case.discharge), 2),
                    ("Cause", "Illness", 1.4),
                ],
            )
            - 6
        )
        y = base._band(pdf, y, "E. DETAILS OF CLAIM")
        y = (
            _form_row(
                pdf,
                case,
                y,
                [
                    ("Hospitalisation expenses (Rs.)", base.inr(case.grand_total), 2),
                    ("Pre / post hospitalisation (Rs.)", "0.00", 2),
                    ("TOTAL CLAIMED (Rs.)", base.inr(case.grand_total), 2),
                ],
            )
            - 6
        )
        y = base._band(pdf, y, "H. BANK ACCOUNT FOR PAYMENT (NEFT)")
        y = (
            _form_row(
                pdf,
                case,
                y,
                [
                    ("Account number", case.account, 2),
                    ("IFSC", case.ifsc, 1.4),
                    ("Account holder", p.name.upper(), 2),
                ],
            )
            - 10
        )
        y = base._band(pdf, y, "I. DECLARATION BY THE INSURED")
        y = base._para(
            pdf,
            y - 4,
            "I declare that the information in this form is true and "
            "correct to the best of my knowledge.",
            size=8.5,
        )
        _value(pdf, case, RIGHT - 160, y - 24, p.name)
        pdf.setFont("Helvetica", 8)
        pdf.drawRightString(RIGHT - 10, y - 38, "Signature of the insured")
        base._footer(pdf, "Claim form Part A, page 1 of 1")
        pdf.showPage()

    return base._save(build)


def claim_form_part_b(case: Case) -> bytes:
    t = case.treatment

    def build(pdf: Canvas) -> None:
        y = base._insurer_header(pdf, "PART B", "HOSPITAL")
        y = base._band(pdf, y, "A. DETAILS OF HOSPITAL")
        y = base._box_row(
            pdf,
            y,
            [
                ("Name of the hospital", case.hosp.name, 3),
                ("Hospital ID (ROHINI)", case.hosp.rohini, 2),
            ],
        )
        y = base._box_row(pdf, y, [("Treating doctor", case.surgeon, 3)]) - 6
        y = base._band(pdf, y, "B. DETAILS OF THE PATIENT ADMITTED")
        y = base._box_row(
            pdf,
            y,
            [
                ("Name of the patient", case.person.name.upper(), 3),
                ("IP registration number", case.ip_number, 2),
            ],
        )
        y = (
            base._box_row(
                pdf,
                y,
                [
                    ("Date of admission", base.dmy(case.admission), 1),
                    ("Date of discharge", base.dmy(case.discharge), 1),
                    ("Total claim amount (Rs.)", base.inr(case.grand_total), 1),
                ],
            )
            - 6
        )
        y = base._band(pdf, y, "C. DETAILS OF AILMENT DIAGNOSED (PRIMARY)")
        y = base._box_row(
            pdf, y, [("ICD-10 code", t.icd_written, 1), ("Description", t.diagnosis, 4)]
        )
        if t.procedure:
            y = base._box_row(
                pdf,
                y,
                [("Procedure", t.procedure, 3), ("Date of procedure", base.dmy(case.on(1)), 1)],
            )
        _seal(pdf, case, RIGHT - 120, y - 70)
        base._footer(pdf, "Claim form Part B, page 1 of 1")
        pdf.showPage()

    return base._save(build)


def _history(case: Case) -> str:
    ped = next((p for p in case.problems if p["type"] == "undeclared_ped"), None)
    return f"{ped['history_text']}." if ped else "Not significant."


def discharge_summary(case: Case) -> bytes:
    t = case.treatment
    return _document(
        case,
        "DISCHARGE SUMMARY",
        [
            (
                "Dates",
                f"Admitted {base.dmy(case.admission)}, discharged {base.dmy(case.discharge)}",
            ),
            ("Final diagnosis", f"{t.diagnosis}    ICD-10: {t.icd_written}"),
            ("Chief complaints", t.complaints),
            ("Past history", _history(case)),
            ("Procedure", f"{t.procedure} on {base.dmy(case.on(1))}" if t.procedure else "None"),
            (
                "Course in hospital",
                t.course
                + (
                    f" Stay extended to {case.stay} days for observation."
                    if case.stay > t.stay_days
                    else ""
                ),
            ),
            ("Condition at discharge", "Stable, afebrile, ambulatory."),
        ],
        "Discharge summary, page 1 of 1",
    )


def ot_notes(case: Case) -> bytes:
    t = case.treatment
    return _document(
        case,
        "OPERATION THEATRE NOTES",
        [
            ("Date of surgery", base.dmy(case.on(1))),
            ("Pre-operative diagnosis", t.diagnosis),
            ("Procedure", t.procedure or ""),
            ("Findings", "Acutely inflamed appendix, no perforation or abscess."),
            ("Specimen", "Sent for histopathology."),
        ],
        "OT notes, page 1 of 1",
    )


def investigation_report(case: Case) -> bytes:
    return _document(
        case,
        "HISTOPATHOLOGY REPORT",
        [
            ("Lab No.", f"HP/{case.admission:%y}/{case.serial}"),
            ("Specimen", "Appendix, received in formalin"),
            ("Microscopy", "Transmural neutrophilic infiltrate with mucosal ulceration."),
            ("Impression", "Acute suppurative appendicitis."),
        ],
        "Histopathology report, page 1 of 1",
    )


def _plain_header(pdf: Canvas, title: str) -> float:
    """A bill printed on blank paper: no hospital name, address or ROHINI ID."""
    pdf.setFont("Courier-Bold", 12)
    pdf.drawCentredString(W / 2, H - 60, title)
    return H - 76


def _bill(case: Case, title: str, number: str, items: list[Item], footer: str) -> bytes:
    plain = case.spec.variant.plain_bill and title.startswith("FINAL")

    def head(pdf: Canvas, text: str) -> float:
        return _plain_header(pdf, text) if plain else _header(pdf, case, text)

    def build(pdf: Canvas) -> None:
        y = head(pdf, title)
        payer = "Cashless (TPA)" if case.spec.route == "cashless" else "Self (reimbursement)"
        y = base._box_row(
            pdf,
            y,
            [
                ("Bill No.", number, 2),
                ("Bill date", base.dmon(case.discharge), 1.3),
                ("Payer", payer, 1.6),
            ],
        )
        y = _patient_row(pdf, case, y) - 8
        page = 1

        def heading(y: float) -> float:
            pdf.setFont("Helvetica-Bold", 8.5)
            for name, x in (("Date", LEFT + 4), ("Particulars", LEFT + 78)):
                pdf.drawString(x, y, name)
            pdf.drawRightString(372, y, "Qty")
            pdf.drawRightString(470, y, "Rate (Rs.)")
            pdf.drawRightString(RIGHT - 4, y, "Amount (Rs.)")
            return y - 16

        y = heading(y)
        for section in SECTIONS:
            rows = sorted((i for i in items if i.section == section), key=lambda i: i.day)
            if not rows:
                continue
            if y < 120 + 12 * len(rows):
                base._footer(pdf, f"{footer}, page {page}")
                pdf.showPage()
                page += 1
                y = heading(head(pdf, title + " contd."))
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(LEFT + 4, y, section)
            y -= 13
            pdf.setFont("Helvetica", 8.6)
            for item in rows:
                pdf.drawString(LEFT + 4, y, base.dmon(case.on(item.day)))
                pdf.drawString(LEFT + 78, y, item.description)
                pdf.drawRightString(372, y, f"{item.qty:g}")
                pdf.drawRightString(470, y, base.inr(item.rate))
                pdf.drawRightString(RIGHT - 4, y, base.inr(item.amount))
                y -= 12
            total = money(sum((i.amount for i in rows), ZERO))
            pdf.setFont("Helvetica-Bold", 8.6)
            pdf.drawRightString(470, y, "Section total")
            pdf.drawRightString(RIGHT - 4, y, base.inr(total))
            y -= 16
        grand = money(sum((i.amount for i in items), ZERO))
        if y < 150:
            base._footer(pdf, f"{footer}, page {page}")
            pdf.showPage()
            page += 1
            y = head(pdf, title + " contd.")
        pdf.line(LEFT, y + 6, RIGHT, y + 6)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(470, y - 8, "GRAND TOTAL")
        pdf.drawRightString(RIGHT - 4, y - 8, base.inr(grand))
        pdf.setFont("Helvetica-Oblique", 8.5)
        pdf.drawString(LEFT + 4, y - 24, f"Amount in words: {base.rupees_in_words(grand)}")
        if not plain:
            _seal(pdf, case, RIGHT - 90, y - 80)
        base._footer(pdf, f"{footer}, page {page}")
        pdf.showPage()

    return base._save(build)


def final_bill(case: Case) -> bytes:
    return _bill(
        case, "FINAL BILL - INPATIENT (DETAILED)", case.bill_number, case.items, "Final bill"
    )


def interim_bill(case: Case) -> bytes:
    planned = case.treatment.stay_days
    running = [i for i in case.items if i.day < planned]
    return _bill(case, "INTERIM BILL (RUNNING)", f"{case.bill_number}/INT", running, "Interim bill")


def cancelled_cheque(case: Case) -> bytes:
    def build(pdf: Canvas) -> None:
        x0, y0, w, h = LEFT + 10, H - 330, RIGHT - LEFT - 20, 240
        pdf.setFillColor(Color(0.93, 0.96, 0.99))
        pdf.rect(x0, y0, w, h, stroke=1, fill=1)
        pdf.setFillColor(black)
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(x0 + 14, y0 + h - 24, bank_name(case.ifsc).upper())
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(x0 + 14, y0 + h - 49, f"IFSC: {case.ifsc}")
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(x0 + 14, y0 + h - 136, f"A/c No. {case.cheque_account}")
        pdf.setFont("Helvetica", 8)
        pdf.drawRightString(x0 + w - 14, y0 + 72, case.person.name.upper())
        pdf.saveState()
        pdf.setStrokeColor(Color(0.75, 0.1, 0.1))
        pdf.setFillColor(Color(0.75, 0.1, 0.1))
        pdf.translate(x0 + w / 2 + 40, y0 + h / 2 + 34)
        pdf.rotate(12)
        pdf.setFont("Helvetica-Bold", 30)
        pdf.drawCentredString(0, -10, "CANCELLED")
        pdf.restoreState()
        base._footer(pdf, "Cancelled cheque")
        pdf.showPage()

    return base._save(build)


def bank_name(ifsc: str) -> str:
    """The bank an IFSC's 4-letter code belongs to (digitisation's own table), so the
    cheque passes its ifsc_bank_code check; BSBL is the tracer's fictitious bank."""
    names = BANK_CODES.get(ifsc[:4])
    return names[0].title() if names else "Bharat Sahakari Bank Ltd."


def payment_receipt(case: Case) -> bytes:
    total = case.grand_total

    def build(pdf: Canvas) -> None:
        y = _header(pdf, case, "PAYMENT RECEIPT")
        y = (
            base._box_row(
                pdf,
                y,
                [
                    ("Receipt No.", f"RCPT/{case.discharge:%y%m}/{case.serial}", 2),
                    ("Against bill No.", case.bill_number, 2),
                ],
            )
            - 20
        )
        base._para(
            pdf,
            y,
            f"Received with thanks from {case.title} {case.person.name} the sum of "
            f"Rs. {base.inr(total)} ({base.rupees_in_words(total)}) in full settlement of "
            f"bill No. {case.bill_number}.",
            size=10,
        )
        _seal(pdf, case, RIGHT - 90, y - 120)
        base._footer(pdf, "Payment receipt")
        pdf.showPage()

    return base._save(build)


def preauth_form(case: Case) -> bytes:
    t = case.treatment
    heads = _estimate(case, t.stay_days)

    def build(pdf: Canvas) -> None:
        y = _header(pdf, case, "REQUEST FOR CASHLESS HOSPITALISATION - PART C")
        y = _patient_row(pdf, case, y)
        y = base._box_row(
            pdf,
            y,
            [("Policy No.", case.person.policy_number, 2), ("Member ID", case.person.member_id, 2)],
        )
        y = base._box_row(
            pdf,
            y,
            [
                ("Treating doctor", case.surgeon, 2),
                ("Provisional diagnosis", t.diagnosis, 2),
                ("ICD-10", t.icd_written, 1),
            ],
        )
        y = base._box_row(
            pdf,
            y,
            [
                ("Proposed treatment", t.procedure or "Medical management", 3),
                ("Admission date", base.dmy(case.admission), 1.3),
                ("Expected stay (days)", str(t.stay_days), 1.1),
            ],
        )
        y = base._band(pdf, y - 6, "ESTIMATED COST OF HOSPITALISATION")
        for line in heads:
            y = base._box_row(
                pdf,
                y,
                [(line.description, "", 3), ("Amount (Rs.)", base.inr(line.claimed), 1.4)],
                height=22,
            )
        total = sum((ln.claimed for ln in heads), ZERO)
        y = base._box_row(
            pdf, y, [("Total estimate", "", 3), ("Amount (Rs.)", base.inr(total), 1.4)]
        )
        _seal(pdf, case, RIGHT - 90, y - 60)
        base._footer(pdf, "Pre-auth Part C, page 1 of 1")
        pdf.showPage()

    return base._save(build)


def enhancement_request(case: Case) -> bytes:
    planned = case.treatment.stay_days
    extra = _estimate(case, case.stay, start=planned)

    def build(pdf: Canvas) -> None:
        y = _patient_row(pdf, case, _header(pdf, case, "REQUEST FOR ENHANCEMENT")) - 10
        y = base._labelled(
            pdf,
            y,
            "REASON",
            f"Slow post-operative recovery; {case.stay - planned} more day(s) of stay.",
        )
        for line in extra:
            y = base._box_row(
                pdf,
                y,
                [(line.description, "", 3), ("Amount (Rs.)", base.inr(line.claimed), 1.4)],
                height=22,
            )
        total = sum((ln.claimed for ln in extra), ZERO)
        base._box_row(pdf, y, [("Extra amount", "", 3), ("Amount (Rs.)", base.inr(total), 1.4)])
        base._footer(pdf, "Enhancement request, page 1 of 1")
        pdf.showPage()

    return base._save(build)


Render = Callable[[Case], bytes]
REIMBURSEMENT_DOCS: list[tuple[DocType, Render]] = [
    (DocType.CLAIM_FORM_PART_A, claim_form_part_a),
    (DocType.CLAIM_FORM_PART_B, claim_form_part_b),
    (DocType.DISCHARGE_SUMMARY, discharge_summary),
    (DocType.OT_NOTES, ot_notes),
    (DocType.INVESTIGATION_REPORT, investigation_report),
    (DocType.FINAL_BILL, final_bill),
    (DocType.PAYMENT_RECEIPT, payment_receipt),
    (DocType.CANCELLED_CHEQUE, cancelled_cheque),
]
CASHLESS_DOCS: dict[str, list[tuple[DocType, Render]]] = {
    "preauth": [
        (DocType.PREAUTH_FORM, preauth_form),
        (DocType.INVESTIGATION_REPORT, investigation_report),
    ],
    "enhancement": [
        (DocType.ENHANCEMENT_REQUEST, enhancement_request),
        (DocType.INTERIM_BILL, interim_bill),
    ],
    "discharge": [
        (DocType.DISCHARGE_SUMMARY, discharge_summary),
        (DocType.OT_NOTES, ot_notes),
        (DocType.FINAL_BILL, final_bill),
    ],
}


# --- Realism variants -------------------------------------------------------------------------

DPI = 130


def _rasterise(data: bytes, index: int) -> Image.Image:
    doc = pdfium.PdfDocument(data)
    try:
        image: Image.Image = doc[index].render(scale=DPI / 72).to_pil().convert("RGB")
        return image
    finally:
        doc.close()


def _noise(rng: random.Random, size: tuple[int, int], spread: int) -> Image.Image:
    """Seeded grey noise around mid-grey (PIL's effect_noise is not seeded)."""
    raw = bytes(128 + rng.randrange(-spread, spread + 1) for _ in range(256 * 256))
    tile = Image.frombytes("L", (256, 256), raw)
    return tile.resize(size, Image.Resampling.NEAREST)


def _scan(image: Image.Image, rng: random.Random) -> Image.Image:
    grey = image.convert("L").rotate(
        rng.uniform(-1.6, 1.6), resample=Image.Resampling.BICUBIC, fillcolor=255
    )
    grey = Image.blend(grey, _noise(rng, grey.size, 60), 0.06)
    draw = ImageDraw.Draw(grey)
    for _ in range(700):
        draw.point(
            (rng.randrange(grey.width), rng.randrange(grey.height)), fill=rng.randrange(60, 170)
        )
    return grey.convert("RGB")


def _photo(image: Image.Image, rng: random.Random) -> Image.Image:
    """A phone photo of the page on a desk: perspective, uneven light, soft focus."""
    w, h = image.size
    pad = int(w * 0.08)
    desk = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (92, 74, 58))
    desk.paste(image, (pad, pad))
    jitter = [rng.uniform(-0.035, 0.035) * w for _ in range(8)]
    quad = (
        jitter[0],
        jitter[1],
        jitter[2],
        desk.height + jitter[3],
        desk.width + jitter[4],
        desk.height + jitter[5],
        desk.width + jitter[6],
        jitter[7],
    )
    warped = desk.transform(
        desk.size, Image.Transform.QUAD, quad, resample=Image.Resampling.BICUBIC
    )
    shade = Image.linear_gradient("L").rotate(rng.uniform(0, 360)).resize(warped.size)
    shade = shade.point(lambda v: 150 + v * 105 // 255)
    warped = Image.composite(warped, Image.new("RGB", warped.size, (25, 20, 15)), shade)
    return warped.filter(ImageFilter.GaussianBlur(rng.uniform(0.6, 1.1)))


_TRAILER_ID = re.compile(rb"/ID\s*\[\s*<([0-9A-Fa-f]{32})>\s*<([0-9A-Fa-f]{32})>\s*\]")


_CREATED = re.compile(rb"(/CreationDate\(D:)\d{14}")


def stable(data: bytes) -> bytes:
    """pdfium writes a random trailer /ID and the save time on a new document. Fix both,
    keeping their lengths so offsets hold, so the same case gives the same bytes."""
    data = _CREATED.sub(rb"\g<1>20000101000000", data)
    digest = hashlib.sha256(_TRAILER_ID.sub(b"", data)).hexdigest()[:32].upper().encode()
    return _TRAILER_ID.sub(b"/ID[<" + digest + b"><" + digest + b">]", data)


def _merge(parts: list[bytes]) -> bytes:
    return stable(base.merge(parts))


def _replace_page(data: bytes, index: int, image: Image.Image) -> bytes:
    jpeg = io.BytesIO()
    image.save(jpeg, format="JPEG", quality=70)
    page = io.BytesIO()
    canvas = base._new_canvas(page)
    canvas.setFillColor(white)
    canvas.drawImage(ImageReader(io.BytesIO(jpeg.getvalue())), 0, 0, width=W, height=H)
    canvas.showPage()
    canvas.save()
    source, replacement = pdfium.PdfDocument(data), pdfium.PdfDocument(page.getvalue())
    out = pdfium.PdfDocument.new()
    try:
        for i in range(len(source)):
            out.import_pages(replacement if i == index else source, [0 if i == index else i])
        return stable(base._pdf_bytes(out))
    finally:
        out.close()
        replacement.close()
        source.close()


def _render(case: Case, doc_type: DocType, render: Render) -> tuple[bytes, list[str]]:
    """The document with its variants applied; returns (bytes, the variants of each page)."""
    variant = case.spec.variant
    data = render(case)
    if doc_type in variant.stamps:
        data = _stamped(case, data)
    count = base.page_count(data)
    marks: list[list[str]] = [[] for _ in range(count)]
    if doc_type in variant.stamps:
        marks[0].append("stamp")
    if doc_type is DocType.CLAIM_FORM_PART_A and variant.handwritten_form:
        marks[0].append("handwritten")
    if doc_type in variant.photo:
        data = _replace_page(data, 0, _photo(_rasterise(data, 0), case.rng))
        marks[0].append("phone_photo")
    elif doc_type in variant.scans:
        index = count - 1
        data = _replace_page(data, index, _scan(_rasterise(data, index), case.rng))
        marks[index].append("scan")
    return data, ["+".join(m) or "clean" for m in marks]


def _stamped(case: Case, data: bytes) -> bytes:
    def draw(pdf: Canvas) -> None:
        _stamp(pdf, case)
        pdf.showPage()

    overlay = base._save(draw)
    source, stamp = pdfium.PdfDocument(data), pdfium.PdfDocument(overlay)
    try:
        xobject = stamp.page_as_xobject(0, source)
        page = source[0]
        page.insert_obj(xobject.as_pageobject())
        page.gen_content()
        return stable(base._pdf_bytes(source))
    finally:
        stamp.close()
        source.close()


# --- Writing a case ---------------------------------------------------------------------------


def generate_case(
    spec: CaseSpec, out_dir: Path, *, today: date, corpus_dir: Path | None = None
) -> dict[str, Any]:
    """Write the case's documents and ground truth to out_dir. Returns the ground truth.
    corpus_dir holds earlier cases, for DUPLICATE_FILE (the reused case's folder)."""
    case = _case(spec, today)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any]
    if spec.route == "reimbursement":
        files = _write_reimbursement(case, out_dir, corpus_dir)
    else:
        files = _write_cashless(case, out_dir)
    rules = DefaultRulesEngine(REPO / "config" / "products")
    truth = ground_truth(case, rules, files)
    if Problem.DUPLICATE_FILE in spec.problems and spec.reuse_doc is DocType.DISCHARGE_SUMMARY:
        # The reused summary names the other case's patient and dates: that is what it says.
        source = _source_truth(spec, corpus_dir)["fields"]
        for path in [k for k in truth["fields"] if k.startswith("clinical.")]:
            truth["fields"][path] = source[path]
    (out_dir / GROUND_TRUTH).write_text(json.dumps(truth, indent=2) + "\n")
    return truth


def _source_truth(spec: CaseSpec, corpus_dir: Path | None) -> dict[str, Any]:
    if corpus_dir is None or spec.reuse_from is None:
        raise ValueError("duplicate_file needs corpus_dir and reuse_from")
    truth: dict[str, Any] = json.loads((corpus_dir / spec.reuse_from / GROUND_TRUTH).read_text())
    return truth


def _reused(spec: CaseSpec, corpus_dir: Path | None, doc_type: DocType) -> bytes:
    assert corpus_dir is not None and spec.reuse_from is not None
    earlier = _source_truth(spec, corpus_dir)
    [item] = [f for f in earlier["files"]["bundle"] if f["doc_type"] == doc_type.value]
    return (corpus_dir / spec.reuse_from / str(item["file"])).read_bytes()


def _write_reimbursement(case: Case, out: Path, corpus_dir: Path | None) -> dict[str, Any]:
    spec = case.spec
    missing = spec.missing if Problem.MISSING_DOCUMENT in spec.problems else None
    bundle: list[dict[str, Any]] = []
    parts: list[bytes] = []
    labels: list[str] = []
    reply = None
    for n, (doc_type, render) in enumerate(REIMBURSEMENT_DOCS, start=1):
        if Problem.DUPLICATE_FILE in spec.problems and doc_type is spec.reuse_doc:
            data, variants = _reused(spec, corpus_dir, doc_type), ["reused"]
            variants *= base.page_count(data)
        else:
            data, variants = _render(case, doc_type, render)
        name = f"{n:02d}_{doc_type.value}.pdf"
        if doc_type is missing:
            name = f"query_reply_{doc_type.value}.pdf"
            (out / name).write_bytes(data)
            reply = {"file": name, "doc_type": doc_type.value, "pages": base.page_count(data)}
            continue
        if spec.variant.delivery == "slots":
            (out / name).write_bytes(data)
        bundle.append(
            {
                "file": name if spec.variant.delivery == "slots" else None,
                "doc_type": doc_type.value,
                "pages": len(variants),
                "page_variants": variants,
            }
        )
        parts.append(data)
        labels += [doc_type.value] * len(variants)
    merged = None
    if spec.variant.delivery == "merged":
        merged = MERGED
        (out / MERGED).write_bytes(_merge(parts))
    return {
        "delivery": spec.variant.delivery,
        "bundle": bundle,
        "merged": merged,
        "merged_pages": labels,
        "reply": reply,
    }


def _write_cashless(case: Case, out: Path) -> dict[str, Any]:
    requests: dict[str, list[dict[str, Any]]] = {}
    n = 0
    for request in case.spec.requests:
        requests[request] = []
        for doc_type, render in CASHLESS_DOCS[request]:
            n += 1
            data, variants = _render(case, doc_type, render)
            name = f"{n:02d}_{request}_{doc_type.value}.pdf"
            (out / name).write_bytes(data)
            requests[request].append(
                {
                    "file": name,
                    "doc_type": doc_type.value,
                    "pages": len(variants),
                    "page_variants": variants,
                }
            )
    return {
        "delivery": "slots",
        "requests": requests,
        "bundle": [],
        "merged": None,
        "merged_pages": [],
        "reply": None,
    }


# The ten approved categories (Vicky, 2026-11-19), one claim each, each reaching a different
# end state. `expect` also tells the eval driver how a person acts at the checkpoints:
# query_reply (answer the query with the reply file), investigator (the verdict), and
# fix_bank (an operator corrects the cheque account to the form's at "fix fields").
# Hospitals and members are spread so RF5 and RF6 fire only where a case seeds them; the
# unanswered-query case runs last, on an app restarted with QUERY_REPLY_DAYS=0.
_CHEQUE, _BILL, _SUMMARY = DocType.CANCELLED_CHEQUE, DocType.FINAL_BILL, DocType.DISCHARGE_SUMMARY
CORPUS: list[CaseSpec] = [
    CaseSpec(
        "c01-clean",
        "clean_reimbursement_paid",
        101,
        "reimbursement",
        policy=1,
        member=2,
        hospital=1,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(),
        variant=Variant(delivery="merged"),
        expect={"final_status": "closed", "decision": "approve"},
    ),
    CaseSpec(
        "c02-room-breach",
        "room_breach_non_payable_partial_crc",
        102,
        "reimbursement",
        policy=3,
        member=1,
        hospital=0,
        treatment="lap_appendicectomy",
        room="deluxe",
        problems=(Problem.HIDDEN_NON_PAYABLE,),
        variant=Variant(delivery="slots", stamps=frozenset({_BILL})),
        expect={"final_status": "closed", "decision": "partial", "crc": True},
    ),
    CaseSpec(
        "c03-missing-doc",
        "missing_document_query_reply_approve",
        103,
        "reimbursement",
        policy=5,
        member=1,
        hospital=2,
        treatment="lap_appendicectomy",
        room="general",
        problems=(Problem.MISSING_DOCUMENT,),
        variant=Variant(delivery="merged", scans=frozenset({_SUMMARY})),
        expect={"final_status": "closed", "decision": "approve", "query_reply": True},
    ),
    CaseSpec(
        "c04-waiting-period",
        "waiting_period_reject_crc",
        104,
        "reimbursement",
        policy=6,
        member=1,
        hospital=3,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.WAITING_PERIOD,),
        variant=Variant(delivery="slots", photo=frozenset({_CHEQUE})),
        expect={"final_status": "closed", "decision": "reject", "crc": True},
    ),
    CaseSpec(
        "c05-duplicate-file",
        "duplicate_file_caught",
        105,
        "reimbursement",
        policy=2,
        member=2,
        hospital=1,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.DUPLICATE_FILE,),
        variant=Variant(delivery="slots"),
        reuse_from="c02-room-breach",
        reuse_doc=_SUMMARY,
        expect={
            "final_status": "closed",
            "decision": "reject",
            "crc": True,
            "investigator": "fraud_confirmed",
        },
    ),
    CaseSpec(
        "c06-fraud-signals",
        "fraud_signals_investigator_referral",
        106,
        "reimbursement",
        policy=5,
        member=2,
        hospital=4,
        treatment="lap_appendicectomy",
        room="deluxe",
        problems=(Problem.INFLATED_BILL, Problem.LONG_STAY),
        variant=Variant(delivery="merged", plain_bill=True),
        expect={
            "final_status": "closed",
            "decision": "partial",
            "crc": True,
            "investigator": "cleared",
            "referred": True,
        },
    ),
    CaseSpec(
        "c07-bank-mismatch",
        "bank_mismatch_operator_fix",
        107,
        "reimbursement",
        policy=1,
        member=1,
        hospital=3,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.BANK_MISMATCH,),
        variant=Variant(delivery="merged", scans=frozenset({_CHEQUE})),
        expect={"final_status": "closed", "decision": "approve", "fix_bank": True},
    ),
    CaseSpec(
        "c08-cashless-unbundling",
        "cashless_unbundling_hospital_paid",
        108,
        "cashless",
        policy=2,
        member=1,
        hospital=0,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.UNBUNDLING,),
        variant=Variant(delivery="slots"),
        extra_days=1,
        expect={"final_status": "closed", "decision": "partial", "crc": True, "payee": "hospital"},
    ),
    CaseSpec(
        "c09-preauth-denied",
        "cashless_preauth_denied",
        109,
        "cashless",
        policy=4,
        member=1,
        hospital=0,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.LAPSED_POLICY,),
        variant=Variant(delivery="slots"),
        requests=("preauth",),
        expect={"final_status": "closed", "decision": "reject", "crc": True},
    ),
    CaseSpec(
        "c10-query-unanswered",
        "query_unanswered_timeout_closure",
        110,
        "reimbursement",
        policy=3,
        member=2,
        hospital=4,
        treatment="lap_appendicectomy",
        room="semi_private",
        problems=(Problem.MISSING_DOCUMENT,),
        variant=Variant(delivery="merged", handwritten_form=True),
        expect={
            "final_status": "closed",
            "decision": "reject",
            "crc": True,
            "query_reply": False,
            "query_reply_days": 0,
        },
    ),
]


def generate_corpus(out: Path, today: date, specs: list[CaseSpec] | None = None) -> list[Any]:
    """Every spec in order (a reused file's source case must come first)."""
    return [
        generate_case(s, out / s.case_id, today=today, corpus_dir=out)
        for s in (CORPUS if specs is None else specs)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    for truth in generate_corpus(args.out, args.today):
        print(
            f"{truth['case_id']:<24} {truth['category']:<28} payable "
            f"{truth['sheet']['payable']:>10}  problems "
            f"{[p['type'] for p in truth['problems']]}"
        )


if __name__ == "__main__":
    main()
