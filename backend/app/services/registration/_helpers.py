"""Pure helpers for registration: name normalising, date overlap and claim numbers."""

import re
from datetime import date

from rapidfuzz import fuzz

HOSPITAL_NAME_MATCH = 90.0  # token_sort_ratio on normalised names
CLAIM_NUMBER = "CLM-{year}-{n:06d}"


def normalise(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def names_match(a: str, b: str) -> bool:
    return fuzz.token_sort_ratio(normalise(a), normalise(b)) >= HOSPITAL_NAME_MATCH


def overlaps(start_a: date, end_a: date | None, start_b: date, end_b: date | None) -> bool:
    """Whether two stays share a day. A stay with no end date lasts one day."""
    return start_a <= (end_b or start_b) and start_b <= (end_a or start_a)


def claim_number(year: int, last: str | None) -> str:
    """The next claim number in the year after the highest one issued so far."""
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return CLAIM_NUMBER.format(year=year, n=n)
