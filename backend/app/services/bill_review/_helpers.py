"""Head mapping and sheet arithmetic for bill review. Pure functions: no I/O."""

import re
from decimal import Decimal

from app.services.bill_review.contract import DeductionLine
from app.services.rules.contract import Head
from app.shared.types import RequestType

ZERO = Decimal("0.00")
# The request types a network hospital sends on a cashless claim.
CASHLESS_REQUESTS = (RequestType.PREAUTH, RequestType.ENHANCEMENT, RequestType.DISCHARGE)

# Patterns per head, matched on whole words of the lower-cased text. Order matters: the
# first head whose pattern matches wins, so ICU comes before room.
HEAD_PATTERNS: list[tuple[Head, str]] = [
    (Head.PACKAGE, r"packages?"),
    (Head.ICU, r"icu|iccu|nicu|picu|hdu|intensive care|critical care"),
    (Head.AMBULANCE, r"ambulance"),
    (Head.IMPLANTS, r"implants?|stents?|prosthe\w*|plates?|screws?|pacemaker"),
    (Head.OT, r"ot|operation theat\w*|operating theat\w*|theat\w* charges?"),
    (Head.NURSING, r"nursing"),
    (
        Head.PROFESSIONAL_FEES,
        r"professional|doctors?|consult\w*|surgeons?|surgeon s|an(?:a)?esthe\w*|"
        r"physician|visits?|dr",
    ),
    (Head.PHARMACY, r"pharmacy|drugs?|medicines?|injections?|tablets?"),
    (Head.CONSUMABLES, r"consumables?|disposables?|surgical items?|sutures?"),
    (
        Head.DIAGNOSTICS,
        r"investigations?|lab|laboratory|pathology|radiology|diagnostics?|x ray|xray|"
        r"ct|mri|ultrasound|usg|scan|ecg|echo|blood tests?|cbc",
    ),
    (Head.ROOM, r"room|rooms|ward|bed charges?|accommodation|boarding"),
]
_COMPILED = [(head, re.compile(rf"\b(?:{pattern})\b")) for head, pattern in HEAD_PATTERNS]


def _text(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (value or "").lower()).split())


def _match(text: str) -> Head | None:
    return next((head for head, pattern in _COMPILED if pattern.search(text)), None)


def map_head(heading: str | None, description: str | None) -> tuple[Head, bool]:
    """The standard head for a bill line: from its section heading, else its description.
    A line neither maps is "other" and flagged for the bill processor."""
    head = _match(_text(heading)) or _match(_text(description))
    return (head, False) if head else (Head.OTHER, True)


# IRDAI Lists II to IV name items that are part of ("subsumed into") another charge: List II
# into the room charge, List III into the procedure charge, List IV into the cost of
# treatment. Such an item is never the charge it is part of, so it is never a candidate for
# a line of that head. A package line is the whole treatment and holds every list.
SUBSUMED_INTO: dict[str, frozenset[Head]] = {
    "II": frozenset({Head.ROOM, Head.ICU, Head.PACKAGE}),
    "III": frozenset({Head.OT, Head.PROFESSIONAL_FEES, Head.PACKAGE}),
    "IV": frozenset({Head.PACKAGE}),
}


def fits_head(list_name: str, head: Head) -> bool:
    """False when an item of this IRDAI list is part of the charge a line of this head is."""
    return head not in SUBSUMED_INTO.get(list_name, frozenset())


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def room_rate_per_day(lines: list[DeductionLine], days: dict[int, Decimal]) -> Decimal | None:
    """The average daily rate charged across the room lines, or None without one."""
    room = [ln for ln in lines if ln.head is Head.ROOM]
    total_days = sum((days[ln.line_id] for ln in room), ZERO)
    if not room or total_days <= 0:
        return None
    return sum((ln.claimed for ln in room), ZERO) / total_days
