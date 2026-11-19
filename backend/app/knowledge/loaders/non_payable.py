"""Non-payable lists I to IV from the Arogya Sanjeevani wording (Go Digit, Annexure A).

The wording prints each IRDAI list item with the product's note on it, for example
"GLOVES (Sterilized Gloves Payable / Unsterilized Gloves not payable)". The loader keeps the
item label as the list names it (a short label) and replaces the note with a condition in our
own words and a payable flag:

- not_payable: never paid as a separate line;
- not_payable_unless: not paid unless the bill shows the stated exception;
- payable_within_limits: paid, within the stated limit;
- payable: paid.

Notes whose meaning is the list's default ("Not Payable", "Part of Room Charge ...") take the
list's condition. Every other note is reviewed by hand below; a note that is not reviewed is
loaded as not_payable_unless and reported, never guessed.

Cross-checks: the New India wording and the IRDAI 2020 circular carry the same lists without
the product notes. The loader compares their labels with the primary's by list and number and
reports the agreement. They are not loaded.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import NonPayableItemRow
from app.knowledge.loaders.common import LoadReport, Sampler, SourceFile, analyze
from app.knowledge.loaders.pdf import (
    NOT_STATED,
    ReferenceRelease,
    clean,
    clear_release,
    lines_of,
    norm_key,
    page_texts,
    percent,
    record_release,
    source_file,
    source_path,
)

PRIMARY = "s1_godigit_arogya_sanjeevani/arogya_sanjeevani_godigit.pdf"
PRIMARY_PAGES = range(20, 26)
CROSS_CHECKS = {
    "New India wording": (
        "s2_newindia_arogya_sanjeevani/arogya_sanjeevani_newindia_policy_clauses.pdf",
        range(22, 26),
    ),
    "IRDAI circular 2020": (
        "s1_irdai_mc_standardization_2020/irdai_master_circular_standardization_2020.pdf",
        range(26, 32),
    ),
}
# The wording does not print the date it is in force from.
RELEASE = ReferenceRelease("non_payable", "GODHLIP24179V022324", NOT_STATED)
FIXTURE = "fixture"

LISTS = ("I", "II", "III", "IV")
LIST_CONDITION = {
    "I": "Not covered by the policy",
    "II": "Part of the room charges; not paid as a separate item",
    "III": "Part of the procedure charges; not paid as a separate item",
    "IV": "Part of the cost of treatment; not paid as a separate item",
}
LIST_CATEGORY = {
    "I": "not covered",
    "II": "room charges",
    "III": "procedure charges",
    "IV": "treatment costs",
}

_ITEM = re.compile(r"^(\d{1,2})\.?\s+(\S.*)$")
_NOISE = re.compile(
    r"^(s|si|sl|sl\.|no|no\.|item|sr\. no.*|s no|sl no\.?|si no\.?)$"
    r"|^list\b|^list[ivx]+\b|^annexure|go digit general insurance|policy wording"
    r"|^page \d+ of \d+$|^uin:|the new india assurance|^\d+\.\s*list\b|items? that are"
    r"|items for which|^\(\d+\)",
    re.IGNORECASE,
)
_STOP = re.compile(r"^list\s*[\u2013-]?\s*v\b|^listv\b|^annexure\s*[\u2013-]?\s*b\b", re.IGNORECASE)
_LIST_HEADING = re.compile(r"^list\s*[\u2013-]?\s*(iv|iii|ii|i)\b", re.IGNORECASE)
# A bracket is the product's note when it holds one of these words, or starts with one of the
# phrases below. Other brackets ("(OTHER THAN ... PART OF BED CHARGE)") stay in the label.
_NOTE_WORDS = re.compile(
    r"payable|reasonabl|device|toiletries|rental|sterili|only actual|prescri|internal cost",
    re.IGNORECASE,
)
_NOTE_START = re.compile(r"^\(?\s*(part of|to be claimed|not payable)", re.IGNORECASE)
_BARE_NOTE = re.compile(r"\s(Not payable|Not Payable|Unless\s|May be Payable)")
# Text extraction reads the slash in this label as an "I"; the New India wording and the IRDAI
# circular both print "SCRUB SOLUTION/STERILLIUM".
_LABEL_FIXES = {
    "SCRUB SOLUTIONISTERILLIUM (Not Payable. Part of hospital's own internal cost)": (
        "SCRUB SOLUTION/STERILLIUM (Not Payable. Part of hospital's own internal cost)"
    ),
}
_TRIVIAL_NOTES = {
    norm_key(n)
    for n in (
        "Not Payable",
        "Part of Room Charge Not Payable Separately",
        "Payable Under OT Charges, Not Payable Separately",
        "Not Payable-Part of Dressing Charges",
        "Part Of OT Charges, Not Separately",
        "Part of Dressing Charges",
        "Device Not Payable",
        "Not Payable. Part of hospital's own internal cost",
        "Part of Nursing Charges, Not Payable",
        "Part of Laundry/housekeeping Not Payable Separately",
        "Not Payable- Part of Room Charges",
        "Payable Under Room Charges Not if separately levied",
        "Part Of Cost Of Blood, Not Payable",
        "Not Payable -Part of Hospital Services/Disposable Linen to be Part of OT/ICU Charges",
    )
}

# (list, label key) -> (flag, condition in our own words). Keyed by label, not number, so a
# wording that renumbers a list still finds its reviewed notes.
_REVIEWED: dict[tuple[str, str], tuple[str, str]] = {
    ("I", "beltsbraces"): (
        "not_payable_unless",
        "Paid only after surgery of the thoracic or lumbar spine",
    ),
    ("I", "leggings"): (
        "not_payable_unless",
        "Paid only with a covered bariatric or varicose vein surgery",
    ),
    ("I", "slings"): (
        "not_payable_unless",
        "One sling at a reasonable cost is paid for an upper arm fracture",
    ),
    ("I", "servicechargeswherenursingchargealsocharged"): (
        "not_payable",
        "Not paid when nursing is also charged",
    ),
    ("I", "televisioncharges"): ("not_payable", "Not paid when levied as a separate item"),
    ("I", "surcharges"): ("not_payable", "Part of the room charges; not paid separately"),
    ("I", "attendantcharges"): (
        "not_payable",
        "Part of the room charges; not paid separately",
    ),
    ("I", "extradietofpatientotherthanthatwhichformspartofbedcharge"): (
        "not_payable",
        "Extra diet is not paid; the patient's own hospital diet is",
    ),
    ("I", "mortuarycharges"): (
        "payable_within_limits",
        "Paid for up to 24 hours; shifting charges are not paid",
    ),
    ("I", "lumbosacralbelt"): (
        "not_payable_unless",
        "Paid only after surgery of the lumbar spine",
    ),
    ("I", "nimbusbedorwaterorairbedcharges"): (
        "not_payable_unless",
        "Paid at about Rs 200 a day only for ICU stays over 3 days or paraplegia or quadriplegia",
    ),
    ("I", "privatenurseschargesspecialnursingcharges"): (
        "payable_within_limits",
        "Special nursing during the stay is paid; nursing after discharge is not",
    ),
    ("I", "sugarfreetablets"): ("payable", "Sugar-free forms of admissible medicines are paid"),
    ("I", "creamspowderslotions"): (
        "not_payable_unless",
        "Toiletries are not paid; only prescribed medicinal products are",
    ),
    ("I", "ecgelectrodes"): (
        "payable_within_limits",
        "Up to 5 electrodes per OT or ICU case, with one set every second day for a longer "
        "ICU stay",
    ),
    ("I", "gloves"): (
        "not_payable_unless",
        "Sterile gloves are paid; non-sterile (examination) gloves are not, so a bill line "
        "must show the gloves were sterile",
    ),
    ("I", "nebulisationkit"): (
        "payable_within_limits",
        "Paid at a reasonable cost when used during the stay",
    ),
    ("I", "anykitwithnodetailsmentioneddeliverykitorthokitrecoverykitetc"): (
        "not_payable",
        "A kit billed without its contents is not paid",
    ),
    ("I", "ambulance"): (
        "payable_within_limits",
        "Paid at a reasonable cost when used during the stay, within the policy's ambulance limit",
    ),
    ("II", "sputumcup"): (
        "not_payable",
        "Paid within investigation charges, not as a consumable",
    ),
    ("II", "luxurytax"): (
        "payable_within_limits",
        "Only the actual government tax is paid; it counts toward the room limit",
    ),
    ("II", "airconditionercharges"): (
        "not_payable",
        "Part of the room charges; not paid when levied separately",
    ),
    ("II", "expensesrelatedtoprescriptionondischarge"): (
        "not_payable",
        "Claimed as post-hospitalisation expenses where admissible, not on this bill",
    ),
    ("III", "disposablesrazorschargesforsitepreparations"): (
        "payable_within_limits",
        "Paid for site preparation",
    ),
    ("III", "dvdcdcharges"): (
        "not_payable_unless",
        "Paid only when the insurer or TPA asked for the CD",
    ),
    ("III", "arthroscopyandendoscopyinstruments"): (
        "not_payable_unless",
        "A hospital's rental charge is paid; buying the instruments is not",
    ),
    ("III", "xrayfilm"): (
        "not_payable",
        "Paid within radiology charges, not as a consumable",
    ),
    ("III", "surgicaltape"): (
        "not_payable",
        "Part of dressing charges; the patient pays when it is prescribed",
    ),
    ("III", "torniquet"): (
        "not_payable",
        "The hospital charges the service; its consumables are not paid separately",
    ),
    ("IV", "hospitalisationforevaluationdiagnosticpurpose"): (
        "not_payable_unless",
        "Not paid unless the claim is accepted under the policy's hospitalisation cover",
    ),
    ("IV", "hydrogenperoxidespiritdisinfectantsetc"): (
        "not_payable_unless",
        "Paid when prescribed for the patient; not for the hospital's own use in the OT, "
        "ward or dressings",
    ),
    ("IV", "nutritionplanningchargesdieticianchargesdietcharges"): (
        "not_payable_unless",
        "Dietician and nutrition planning charges are not paid; the patient's own hospital diet is",
    ),
    ("IV", "hivkit"): ("not_payable_unless", "Paid only as pre-operative screening"),
    ("IV", "antisepticmouthwash"): ("payable_within_limits", "Paid when prescribed"),
    ("IV", "lozenges"): ("payable_within_limits", "Paid when prescribed"),
    ("IV", "mouthpaint"): ("payable_within_limits", "Paid when prescribed"),
    ("IV", "vaccinationcharges"): ("not_payable", LIST_CONDITION["IV"]),
    ("IV", "glucometerstrips"): (
        "not_payable_unless",
        "The device is not paid; strips are paid during the stay with the sugar charts, "
        "never before or after it",
    ),
    ("IV", "urinebag"): (
        "payable_within_limits",
        "Paid where medically necessary, at most one bag in 24 hours",
    ),
}


@dataclass(frozen=True)
class ListItem:
    list_name: str  # "I" to "IV"
    number: int
    text: str  # the label with the product's note, as printed
    page: int


@dataclass(frozen=True)
class Classified:
    label: str
    flag: str
    condition: str
    reviewed: bool  # False when the note is neither the list default nor reviewed by hand
    note_key: str


def parse_lists(pages: Iterable[tuple[int, str]]) -> list[ListItem]:
    """The items of lists I to IV in page order. A list starts at item 1; a number that does
    not continue the current list (a paragraph, a page number) is skipped with the lines that
    follow it, until the next item."""
    items: list[ListItem] = []
    list_index = -1
    expected = 1
    skipping = False
    current: dict[str, object] | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            items.append(
                ListItem(
                    list_name=LISTS[int(current["list"])],  # type: ignore[call-overload]
                    number=int(current["number"]),  # type: ignore[call-overload]
                    text=clean(str(current["text"])),
                    page=int(current["page"]),  # type: ignore[call-overload]
                )
            )
        current = None

    for page, text in pages:
        for line in lines_of(text):
            if _STOP.search(line):
                close()
                return items
            if _NOISE.search(line):
                continue
            match = _ITEM.match(line)
            if match:
                number = int(match[1])
                if number == 1 and list_index < len(LISTS) - 1:
                    close()
                    list_index += 1
                    expected = 2
                    skipping = False
                    current = {"list": list_index, "number": 1, "text": match[2], "page": page}
                    continue
                if number == expected and list_index >= 0:
                    close()
                    expected += 1
                    skipping = False
                    current = {
                        "list": list_index,
                        "number": number,
                        "text": match[2],
                        "page": page,
                    }
                    continue
                close()
                skipping = True
                continue
            if current is not None and not skipping:
                current["text"] = f"{current['text']} {line}"
    close()
    return items


def split_note(text: str) -> tuple[str, str]:
    """(label, note). The note starts at the first bracket that reads as a payment note, or at
    a bare "Not payable" / "Unless" phrase, whichever comes first."""
    text = _LABEL_FIXES.get(clean(text), clean(text))
    starts = [i for i in (_note_bracket(text), _bare_note(text)) if i is not None]
    if not starts:
        return text, ""
    start = min(starts)
    if text[start] == "(":
        return _strip(text[:start]), _strip(text[start + 1 :], bracket=True)
    return _strip(text[:start]), _strip(text[start:])


def _note_bracket(text: str) -> int | None:
    depth, start = 0, -1
    for index, char in enumerate(text):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0 and _is_note(text[start:index]):
                return start
    if depth and start >= 0 and _is_note(text[start:]):
        return start  # an unclosed bracket runs to the end
    return None


def _is_note(bracket: str) -> bool:
    return bool(_NOTE_WORDS.search(bracket) or _NOTE_START.search(bracket))


def _bare_note(text: str) -> int | None:
    match = _BARE_NOTE.search(text)
    return match.start() + 1 if match else None


def _strip(text: str, bracket: bool = False) -> str:
    text = text.strip()
    if bracket and text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1]
    return text.strip()


def classify(item: ListItem) -> Classified:
    label, note = split_note(item.text)
    key = norm_key(label)
    reviewed = _REVIEWED.get((item.list_name, key))
    if reviewed is not None:
        flag, condition = reviewed
        return Classified(label, flag, condition, True, norm_key(note))
    if not note or norm_key(note) in _TRIVIAL_NOTES:
        return Classified(label, "not_payable", LIST_CONDITION[item.list_name], True, "")
    return Classified(
        label,
        "not_payable_unless",
        f"Conditions apply; see the wording (page {item.page})",
        False,
        norm_key(note),
    )


@dataclass(frozen=True)
class Comparison:
    compared: int
    agreed: int
    only_primary: int
    only_other: int
    differences: list[str]

    def line(self) -> str:
        return (
            f"labels agree {percent(self.agreed, self.compared)}; only in primary "
            f"{self.only_primary}; only in copy {self.only_other}"
        )


def compare_lists(primary: list[ListItem], other: list[ListItem]) -> Comparison:
    """Agreement of item labels by (list, number), ignoring spacing and punctuation."""
    mine = {(i.list_name, i.number): split_note(i.text)[0] for i in primary}
    theirs = {(i.list_name, i.number): split_note(i.text)[0] for i in other}
    both = sorted(set(mine) & set(theirs), key=lambda k: (LISTS.index(k[0]), k[1]))
    differences = [
        f"{k[0]}.{k[1]}: {mine[k]!r} vs {theirs[k]!r}"
        for k in both
        if norm_key(mine[k]) != norm_key(theirs[k])
    ]
    return Comparison(
        compared=len(both),
        agreed=len(both) - len(differences),
        only_primary=len(set(mine) - set(theirs)),
        only_other=len(set(theirs) - set(mine)),
        differences=differences,
    )


async def load_non_payable(
    sessions: async_sessionmaker[AsyncSession], source_dir: Path
) -> LoadReport:
    """Replace the wording's non-payable rows in one transaction and remove the fixture."""
    primary_file = source_file(source_dir, PRIMARY, "pp.20-25 Annexure A")
    items = parse_lists(page_texts(source_path(source_dir, PRIMARY), PRIMARY_PAGES))
    report = LoadReport(system=RELEASE.system, version=RELEASE.version)
    counts = {name: sum(1 for i in items if i.list_name == name) for name in LISTS}
    if any(count == 0 for count in counts.values()):
        raise ValueError(f"lists not found in {PRIMARY}: {counts}")

    classified = [(item, classify(item)) for item in items]
    files: list[SourceFile] = [primary_file]
    for name, (relative, pages) in CROSS_CHECKS.items():
        if not (source_dir / relative).is_file():
            report.checks[f"cross-check {name}"] = "file not available"
            continue
        files.append(
            source_file(
                source_dir, relative, f"pp.{pages.start}-{pages.stop - 1} cross-check, not loaded"
            )
        )
        comparison = compare_lists(items, parse_lists(page_texts(source_dir / relative, pages)))
        report.checks[f"cross-check {name}"] = comparison.line()
        for number, difference in enumerate(comparison.differences[:5], 1):
            report.checks[f"cross-check {name} difference {number}"] = difference

    sample = Sampler()
    async with sessions.begin() as session:
        await clear_release(session, RELEASE, NonPayableItemRow)
        await session.execute(delete(NonPayableItemRow).where(NonPayableItemRow.source == FIXTURE))
        for item, c in classified:
            session.add(
                NonPayableItemRow(
                    list_name=item.list_name,
                    item_number=item.number,
                    item=c.label,
                    category=LIST_CATEGORY[item.list_name],
                    condition=c.condition,
                    payable_in_product=c.flag,
                    page=item.page,
                    source=RELEASE.system,
                    source_version=RELEASE.version,
                )
            )
            sample.offer(
                f"List {item.list_name} {item.number:>2} p{item.page} {c.label!r} "
                f"{c.flag}: {c.condition}"
            )
        primary_file.rows = len(classified)
        record_release(session, RELEASE, files)
        await session.flush()
        await analyze(session, "non_payable_items", "kb_source_loads")

    report.files = files
    report.counts = {f"List {name}": count for name, count in counts.items()}
    flags: dict[str, int] = {}
    for _, c in classified:
        flags[c.flag] = flags.get(c.flag, 0) + 1
    report.counts.update({f"flag {flag}": n for flag, n in sorted(flags.items())})
    unreviewed = [f"{i.list_name}.{i.number} {c.label}" for i, c in classified if not c.reviewed]
    report.checks["notes not reviewed (loaded as not_payable_unless)"] = (
        ", ".join(unreviewed) or "none"
    )
    report.sample = sample.rows
    return report
