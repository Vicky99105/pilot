"""Arogya Sanjeevani wording (Go Digit, UIN GODHLIP24179V022324): clause IDs with our own
summaries, the limits the wording states, and its specified-condition lists.

Nothing of the wording's text is stored. Each clause row is an ID from the wording's own
numbering, a short title and a summary we wrote; the loader finds the clause in the wording
(by its heading) and records the page, and reports a clause it cannot find. The source PDF stays
in .local.

The limits (room and ICU caps, cataract, modern treatments, ambulance, pre and post
hospitalisation windows, co-payment, the pre-existing disease wait) are read from the wording
and compared with the product config whose wording_uin is this wording. The rules read the
config; the loader proves it says what the wording says. The specified-condition lists (Excl02)
are compared item by item with the config's specific_waiting list, which carries the ICD ranges.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import PolicyClauseRow
from app.knowledge.loaders.common import LoadReport, analyze
from app.knowledge.loaders.pdf import (
    NOT_STATED,
    ReferenceRelease,
    clear_release,
    lines_of,
    norm_key,
    page_count,
    page_texts,
    record_release,
    source_file,
    source_path,
)
from app.services.rules.contract import ProductConfig

WORDING = "s1_godigit_arogya_sanjeevani/arogya_sanjeevani_godigit.pdf"
UIN = "GODHLIP24179V022324"
RELEASE = ReferenceRelease("policy_wording", UIN, NOT_STATED)

# (clause ID, title, summary in our own words, heading pattern that finds it in the wording)
CLAUSES: list[tuple[str, str, str, str]] = [
    (
        "B.17",
        "Grace period",
        "A renewal or instalment premium may be paid up to 15 days late when paid monthly and "
        "30 days late otherwise, keeping continuity; cover is not given for the unpaid period.",
        r"17\. Grace Period means",
    ),
    (
        "B.34",
        "Pre-existing disease",
        "A condition diagnosed, or for which advice or treatment was recommended or received, "
        "within 36 months before the first policy with the insurer.",
        r"34\. Pre-Existing Disease",
    ),
    (
        "C.I.1",
        "Hospitalisation",
        "Pays medically necessary in-patient expenses up to the sum insured and bonus: room, "
        "boarding and nursing up to 2% of sum insured a day (at most Rs 5,000); ICU or ICCU up "
        "to 5% a day (at most Rs 10,000); doctors' fees; anaesthesia, blood, oxygen, OT, "
        "appliances, medicines and diagnostics.",
        r"1\. Hospitalization",
    ),
    (
        "C.I.1.1",
        "Other expenses",
        "Also pays cataract treatment within its sub-limit, dental and plastic surgery needed "
        "because of disease or injury, all day care treatments, and road ambulance up to "
        "Rs 2,000 per hospitalisation.",
        r"1\.1 Other expenses",
    ),
    (
        "C.I.1-N1",
        "Minimum stay",
        "A hospitalisation must last at least 24 consecutive hours, except day care treatment.",
        r"minimum period of 24 consecutive hours",
    ),
    (
        "C.I.1-N2",
        "Proportionate deduction",
        "When the room or ICU rate charged exceeds its limit, other hospital expenses are paid "
        "in the same proportion as the limit bears to the rate charged, except pharmacy and "
        "consumables, implants and devices, and diagnostics; not applied where the hospital "
        "does not bill differently by room category.",
        r"same proportion as the admissible rate",
    ),
    (
        "C.I.2",
        "AYUSH treatment",
        "In-patient treatment under Ayurveda, Yoga and Naturopathy, Unani, Siddha or "
        "Homeopathy in an AYUSH hospital is paid up to the sum insured.",
        r"2\. AYUSH Treatment",
    ),
    (
        "C.I.3",
        "Cataract",
        "Cataract treatment is paid up to 25% of the sum insured or Rs 40,000, whichever is "
        "lower, for each eye in a policy year.",
        r"3\. Cataract Treatment",
    ),
    (
        "C.I.4",
        "Pre-hospitalisation",
        "Expenses in the 30 days before an admissible hospitalisation, for the same condition, "
        "are paid.",
        r"4\. Pre-Hospitalization",
    ),
    (
        "C.I.5",
        "Post-hospitalisation",
        "Expenses in the 60 days after discharge from an admissible hospitalisation, for the "
        "same condition, are paid.",
        r"5\. Post Hospitalisation",
    ),
    (
        "C.I.6",
        "Modern treatments",
        "Listed modern treatments (such as robotic surgery, stem cell therapy, oral "
        "chemotherapy, immunotherapy, intravitreal injections, deep brain stimulation) are paid "
        "up to 50% of the sum insured, in-patient or day care.",
        r"6\. The following procedures will be covered",
    ),
    (
        "C.I.7",
        "Non-payable items",
        "Items in List I of Annexure A are not covered; items in Lists II, III and IV are part "
        "of room charges, procedure charges and treatment costs respectively.",
        r"7\. The expenses that are not covered",
    ),
    (
        "C.II",
        "Cumulative bonus",
        "The sum insured grows by 5% for each claim-free year, to at most 50%, if renewed "
        "without a break; a claim reduces it at the same rate.",
        r"II\. CUMULATIVE BONUS",
    ),
    (
        "D.I.3",
        "Specific waiting period",
        "The listed conditions and procedures wait 24 or 36 months of continuous cover, "
        "except after an accident (code Excl02).",
        r"3\. Specific Waiting Period",
    ),
    (
        "E.I.1",
        "Disclosure of information",
        "Misrepresentation or non-disclosure of a material fact makes the policy void and the "
        "premium forfeit.",
        r"1\. Disclosure of Information",
    ),
    (
        "E.I.2",
        "Condition precedent",
        "The insured person must meet the policy's terms before any claim is paid.",
        r"2\. Condition Precedent",
    ),
    (
        "E.I.4",
        "Multiple policies",
        "With several policies the insured chooses which to claim under; that insurer settles "
        "within its terms and coordinates with the others for any balance.",
        r"4\. Multiple Policies",
    ),
    (
        "E.I.5",
        "Fraud",
        "A fraudulent claim, or one backed by a false statement or document, forfeits all "
        "benefits, and amounts paid on it must be repaid; an honest misstatement is not fraud.",
        r"5\. Fraud",
    ),
    (
        "E.I.6",
        "Cancellation",
        "The insured may cancel on 7 days' notice for a refund of unexpired premium if no claim "
        "was made; the insurer may cancel on 7 days' notice for misrepresentation, "
        "non-disclosure or fraud, without refund.",
        r"6\. Cancellation",
    ),
    (
        "E.I.8",
        "Portability",
        "Credits for waiting periods, bonus and sum insured transfer to a new insurer on porting.",
        r"8\. Portability",
    ),
    (
        "E.I.9",
        "Renewal",
        "Renewal is not refused because of earlier claims; a renewal within the 30-day grace "
        "period keeps continuity but gives no cover for the gap.",
        r"9\. Renewal of Policy",
    ),
    (
        "E.I.10",
        "Premium in instalments",
        "With instalments, cover lapses from the due date until the premium is received within "
        "the grace period, and pending instalments may be deducted from a claim.",
        r"10\. Premium Payment in Instalments",
    ),
    (
        "E.I.12",
        "Free look period",
        "A new policy may be returned within 30 days for a refund less costs, if no claim was "
        "made.",
        r"12\. Free look period",
    ),
    (
        "E.I.14",
        "Moratorium",
        "After 60 continuous months of cover, no claim can be contested for non-disclosure or "
        "misrepresentation, only for established fraud.",
        r"14\. Moratorium Period",
    ),
    (
        "E.I.16",
        "Claim settlement and penal interest",
        "A claim is settled or rejected within 15 days of submission, investigation included; "
        "a delay earns interest at 2% above the bank rate.",
        r"16\. Claim Settlement",
    ),
    (
        "E.I.17",
        "Grievance redressal",
        "Complaints go to the insurer, then its grievance officer, then the Insurance "
        "Ombudsman or the IRDAI portal.",
        r"17\. REDRESSAL OF GRIEVANCE",
    ),
    (
        "E.II.21",
        "Territorial limit",
        "Treatment must be taken in India.",
        r"21\. Territorial Limit",
    ),
    (
        "E.II.26",
        "Change of sum insured",
        "The sum insured changes at renewal, subject to underwriting; waiting periods restart "
        "for any increase.",
        r"26\. Change of Sum Insured",
    ),
    (
        "E.II.28.1",
        "Cashless claims",
        "Cashless treatment at a network hospital needs pre-authorisation; the insured pays "
        "non-medical and inadmissible items at discharge, and may claim reimbursement if "
        "cashless is denied.",
        r"1\. Procedure for Cashless claims",
    ),
    (
        "E.II.28.2",
        "Reimbursement claims",
        "Hospitalisation and pre-hospitalisation claims are filed within 15 days of discharge, "
        "post-hospitalisation claims within 15 days of finishing that treatment.",
        r"2\. Procedure for reimbursement of claims",
    ),
    (
        "E.II.28.a",
        "Notification of claim",
        "An emergency admission is notified within 24 hours or before discharge, whichever is "
        "earlier; a planned one at least 48 hours before admission.",
        r"a\. Notification of Claim",
    ),
    (
        "E.II.28.b",
        "Claim documents",
        "A reimbursement claim needs the claim form, photo ID, admission advice, itemised "
        "bills and receipts, discharge summary, reports, OT notes, implant stickers, MLC or "
        "FIR where relevant, bank details with a cancelled cheque, and KYC above Rs 1 lakh.",
        r"b\. Documents to be submitted",
    ),
    (
        "E.II.28.c",
        "Co-payment",
        "Every claim bears a 5% co-payment on the admissible amount, deducted from what is paid.",
        r"c\. Co-payment",
    ),
    (
        "ANNEX-A",
        "Annexure A: non-payable lists",
        "Lists I to IV of items not covered or included in room, procedure or treatment "
        "charges, with the product's notes on when an item is paid.",
        r"ANNEXURE A",
    ),
]

# name -> (pattern, groups to read); each group is one number. Searched on each page's text
# with its lines joined, so a phrase that wraps still matches.
_LIMITS: dict[str, tuple[str, tuple[str, ...]]] = {
    "room": (
        r"Room Rent, Boarding, Nursing Expenses as provided by the Hospital / Nursing Home up to "
        r"(\d+)% of the sum insured subject to maximum of Rs\.?\s*([\d,]+)",
        ("room_percent", "room_max_per_day"),
    ),
    "icu": (
        r"\(ICCU\) expenses up to (\d+)% of sum insured subject to maximum of Rs\.?\s*([\d,]+)",
        ("icu_percent", "icu_max_per_day"),
    ),
    "ambulance": (
        r"road Ambulance subject to a maximum of Rs\.?\s*([\d,]+)",
        ("ambulance_max",),
    ),
    "cataract": (
        r"Cataract, subject to a limit of (\d+)% of Sum Insured or Rs\.?\s*([\d,]+)",
        ("cataract_percent", "cataract_max_per_eye"),
    ),
    "pre": (r"fixed period of (\d+) days prior to the date", ("pre_days",)),
    "post": (r"fixed period of (\d+) days from the date of discharge", ("post_days",)),
    "modern": (
        r"day care treatment in a hospital up to (\d+)% of Sum Insured",
        ("modern_treatments_percent",),
    ),
    "ped": (
        r"\(Code- Excl01\).{0,200}?expiry of (\d+)\s*months",
        ("ped_months",),
    ),
    "co_pay": (r"Co-payment of (\d+)% applicable", ("co_pay_percent",)),
}

_WAIT_HEADING = re.compile(r"^(?:i|ii)\.\s*(\d+)\s*Months waiting period", re.IGNORECASE)
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")
_HEADER = re.compile(r"go digit general insurance|policy wording \(", re.IGNORECASE)


@dataclass(frozen=True)
class WaitingItem:
    item: str
    months: int
    page: int


def find_limits(pages: Iterable[tuple[int, str]]) -> dict[str, tuple[Decimal, int]]:
    """Each limit the wording states: name -> (value, page)."""
    found: dict[str, tuple[Decimal, int]] = {}
    for page, text in pages:
        joined = " ".join(lines_of(text))
        for pattern, names in _LIMITS.values():
            match = re.search(pattern, joined, re.IGNORECASE)
            if not match:
                continue
            for name, value in zip(names, match.groups(), strict=True):
                found.setdefault(name, (Decimal(value.replace(",", "")), page))
    return found


def parse_waiting_lists(pages: Iterable[tuple[int, str]]) -> list[WaitingItem]:
    """The numbered conditions under each "N Months waiting period" heading, until the next
    exclusion (a line with an Excl code) ends the lists."""
    items: list[WaitingItem] = []
    months: int | None = None
    current: list[object] | None = None  # [text, months, page]

    def close() -> None:
        nonlocal current
        if current is not None:
            items.append(WaitingItem(str(current[0]), int(current[1]), int(current[2])))  # type: ignore[call-overload]
        current = None

    for page, text in pages:
        for line in lines_of(text):
            if _HEADER.search(line):
                continue
            heading = _WAIT_HEADING.match(line)
            if heading:
                close()
                months = int(heading[1])
                continue
            if months is None:
                continue
            if re.search(r"\(Code\s*-?\s*Excl", line):
                close()
                return items
            numbered = _NUMBERED.match(line)
            if numbered:
                close()
                current = [numbered[2], months, page]
            elif current is not None:
                current[0] = f"{current[0]} {line}"
    close()
    return items


def check_config(product: ProductConfig, limits: dict[str, tuple[Decimal, int]]) -> dict[str, str]:
    """For each limit: the wording's value and page, the config's value, and whether they
    agree."""
    sub = product.sub_limits
    pairs: dict[str, tuple[str, Decimal | int | None]] = {
        "room cap percent": ("room_percent", product.room_rent.percent_of_sum_insured),
        "room cap per day": ("room_max_per_day", product.room_rent.max_per_day),
        "ICU cap percent": ("icu_percent", product.icu.percent_of_sum_insured),
        "ICU cap per day": ("icu_max_per_day", product.icu.max_per_day),
        "cataract percent": (
            "cataract_percent",
            sub.cataract.percent_of_sum_insured if sub else None,
        ),
        "cataract per eye": ("cataract_max_per_eye", sub.cataract.max_per_eye if sub else None),
        "modern treatments percent": (
            "modern_treatments_percent",
            sub.modern_treatments_percent_of_sum_insured if sub else None,
        ),
        "ambulance per hospitalisation": (
            "ambulance_max",
            sub.ambulance_max_per_hospitalisation if sub else None,
        ),
        "pre-hospitalisation days": ("pre_days", product.pre_hospitalisation_days),
        "post-hospitalisation days": ("post_days", product.post_hospitalisation_days),
        "pre-existing disease months": ("ped_months", product.pre_existing_waiting_months),
        "co-pay percent": ("co_pay_percent", product.co_pay.percent),
    }
    lines: dict[str, str] = {}
    for label, (name, configured) in pairs.items():
        if name not in limits:
            lines[label] = f"not found in the wording; config {configured}: UNVERIFIED"
            continue
        value, page = limits[name]
        agree = configured is not None and Decimal(configured) == value
        lines[label] = (
            f"wording {value} p{page}, config {configured}: {'agree' if agree else 'DIFFERS'}"
        )
    return lines


def check_waiting(product: ProductConfig, items: list[WaitingItem]) -> dict[str, str]:
    """The config's specified conditions against the wording's, in order, with the months."""
    configured = product.specific_waiting.conditions if product.specific_waiting else []
    wording = [(norm_key(i.item), i.months) for i in items]
    config = [(norm_key(c.item), c.months) for c in configured]
    missing = [i.item for i in items if (norm_key(i.item), i.months) not in config]
    extra = [c.item for c in configured if (norm_key(c.item), c.months) not in wording]
    return {
        "specified conditions": f"wording {len(items)}, config {len(configured)}",
        "in the wording, not in the config": "; ".join(missing) or "none",
        "in the config, not in the wording": "; ".join(extra) or "none",
    }


def products_for(config_dir: Path, uin: str) -> list[ProductConfig]:
    products = []
    for path in sorted((config_dir / "products").glob("*.yaml")):
        product = ProductConfig.model_validate(yaml.safe_load(path.read_text()))
        if product.wording_uin == uin:
            products.append(product)
    return products


async def load_policy_wording(
    sessions: async_sessionmaker[AsyncSession],
    source_dir: Path,
    config_dir: Path | None = None,
) -> LoadReport:
    path = source_path(source_dir, WORDING)
    wording = source_file(source_dir, WORDING, "all pages")
    pages = list(page_texts(path, range(1, page_count(path) + 1)))
    joined = {page: " ".join(lines_of(text)) for page, text in pages}
    found: dict[str, int] = {}
    for ref, _, _, pattern in CLAUSES:
        for page, text in joined.items():
            if re.search(pattern, text):
                found[ref] = page
                break
    limits = find_limits(pages)
    waiting = parse_waiting_lists(pages)

    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, PolicyClauseRow)
        for ref, title, summary, _ in CLAUSES:
            if ref not in found:
                continue
            session.add(
                PolicyClauseRow(
                    product_uin=UIN,
                    clause_ref=ref,
                    title=title,
                    text=summary,
                    page=found[ref],
                    source=RELEASE.system,
                    source_version=RELEASE.version,
                )
            )
        wording.rows = len(found)
        record_release(session, RELEASE, [wording])
        await session.flush()
        await analyze(session, "policy_clauses", "kb_source_loads")

    report.files = [wording]
    report.counts = {
        "clauses": len(found),
        "limits read": len(limits),
        "specified conditions": len(waiting),
    }
    report.checks["clauses not found in the wording"] = (
        ", ".join(ref for ref, *_ in CLAUSES if ref not in found) or "none"
    )
    for product in products_for(config_dir or Settings().config_dir, UIN):
        for label, line in check_config(product, limits).items():
            report.checks[f"{product.product_uin} {label}"] = line
        for label, line in check_waiting(product, waiting).items():
            report.checks[f"{product.product_uin} {label}"] = line
    report.sample = [
        f"{ref} p{found[ref]} {title}: {summary[:60]}..."
        for ref, title, summary, _ in CLAUSES
        if ref in found
    ][:12] + [f"Excl02 {w.months} months p{w.page}: {w.item}" for w in waiting][:8]
    return report
