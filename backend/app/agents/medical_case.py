"""The medical review case file and the code-side checks on it. Pure functions, no I/O."""

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import TypeVar

from pydantic import BaseModel

from app.services.digitisation.contract import FactKind
from app.services.medical_review.contract import (
    CLAUSE_PREFIX,
    ICD_PREFIX,
    Citation,
    Finding,
)
from app.shared.types import Code, DocType, PageRef

LONG_STAY_DAYS = 7  # no norms table in the tracer: longer stays need a stated reason
PED_CLAUSE = "Excl01"
_UNIT_MONTHS = {"year": 12, "yr": 12, "month": 1, "mth": 1, "mo": 1}
_UNIT_DAYS = {"week": 7, "wk": 7, "day": 1}

ItemT = TypeVar("ItemT")


def unique(items: list[ItemT]) -> list[ItemT]:
    """Items in order, without repeats. Works for unhashable pydantic models."""
    kept: list[ItemT] = []
    for item in items:
        if item not in kept:
            kept.append(item)
    return kept


class CaseFact(BaseModel):
    kind: FactKind
    text: str
    duration: str | None = None
    source: PageRef | None = None


class CaseFile(BaseModel):
    """Everything the medical review graph may read."""

    diagnoses: list[CaseFact]  # each diagnosis phrase as written, with its page
    facts: list[CaseFact]  # every page-tagged clinical fact
    hospital_codes: list[Code]  # as the hospital wrote them; unverified
    admission_date: date | None
    discharge_date: date | None
    date_sources: list[PageRef]  # pages the stay dates came from
    inception_date: date
    declared_peds: list[Code]
    known_pages: list[PageRef]  # the pages a citation may name
    clause_refs: list[str]  # the policy clauses a citation may name
    # Document types a query may ask for: medical documents the claim does not hold and the
    # claimant has not already been asked for and answered.
    requestable: list[DocType] = []


# --- Citations ----------------------------------------------------------------------------


def citation_problem(citation: Citation, case: CaseFile, looked_up: set[str]) -> str | None:
    """Why a citation is not valid, or None when it is."""
    if citation.page is not None:
        if citation.page in case.known_pages:
            return None
        page = citation.page
        return f"page {page.page} of document {page.document_id} is not a page of the claim"
    ref = citation.ref or ""
    if ref.startswith(CLAUSE_PREFIX):
        clause = ref.removeprefix(CLAUSE_PREFIX)
        return None if clause in case.clause_refs else f"no policy clause {clause}"
    if ref.startswith(ICD_PREFIX):
        code = ref.removeprefix(ICD_PREFIX).upper()
        return None if code in looked_up else f"{code} was not looked up"
    return f"unknown citation {ref!r}"


def valid_citations(
    citations: list[Citation], case: CaseFile, looked_up: set[str]
) -> list[Citation]:
    return [c for c in citations if citation_problem(c, case, looked_up) is None]


# --- Hospital codes -----------------------------------------------------------------------


def _norm(code: str) -> str:
    return code.strip().upper()


def diagnosis_pages(case: CaseFile) -> list[Citation]:
    pages = [d.source for d in case.diagnoses if d.source is not None]
    return [Citation(page=p) for p in unique(pages)] or _first_pages(case)


def _first_pages(case: CaseFile) -> list[Citation]:
    return [Citation(page=p) for p in case.known_pages if p.page == 1][:1]


def compare_hospital_codes(case: CaseFile, verified: list[str]) -> list[Finding]:
    """One info finding per hospital code the verified codes do not confirm."""
    findings = []
    chosen = [_norm(c) for c in verified]
    for written in case.hospital_codes:
        code = _norm(written.code)
        if code in chosen:
            continue
        finer = [c for c in chosen if c.startswith(code)]
        if finer:
            text = f"hospital code {code} is less specific than the verified {', '.join(finer)}"
        elif chosen:
            text = f"hospital code {code} differs from the verified {', '.join(chosen)}"
        else:
            text = f"hospital code {code} could not be verified"
        findings.append(
            Finding(
                kind="hospital_codes",
                text=text,
                severity="info",
                citations=diagnosis_pages(case),
                judged_by="code",
            )
        )
    return findings


# --- Length of stay -----------------------------------------------------------------------


def length_of_stay(case: CaseFile, reason_cited: bool) -> list[Finding]:
    """A stay over LONG_STAY_DAYS with no cited reason is flagged for information."""
    if case.admission_date is None or case.discharge_date is None:
        return []
    days = (case.discharge_date - case.admission_date).days
    if days <= LONG_STAY_DAYS or reason_cited:
        return []
    citations = [Citation(page=p) for p in unique(case.date_sources)] or _first_pages(case)
    return [
        Finding(
            kind="length_of_stay",
            text=(
                f"stay of {days} days ({case.admission_date} to {case.discharge_date}) is over "
                f"{LONG_STAY_DAYS} days and no reason is stated"
            ),
            severity="info",
            citations=citations,
            judged_by="code",
        )
    ]


# --- Pre-existing disease -----------------------------------------------------------------


def _minus_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def onset(duration: str, as_of: date) -> date | None:
    """The latest date the condition can have started, from a phrase such as "since 5
    years", "for 3 months" or "since 2019". None when the phrase gives no duration."""
    text = duration.lower()
    year = re.search(r"\b(?:since|from|in)\s+((?:19|20)\d{2})\b", text)
    if year:
        return date(int(year.group(1)), 12, 31)
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mths?|mos?|weeks?|wks?|days?)\b", text
    )
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).rstrip("s")
    if unit in _UNIT_MONTHS:
        return _minus_months(as_of, int(amount * _UNIT_MONTHS[unit]))
    return as_of - timedelta(days=int(amount * _UNIT_DAYS[unit]))


def pre_existing(case: CaseFile) -> list[Finding]:
    """Facts whose stated duration puts their start before the policy inception."""
    if case.admission_date is None:
        return []
    findings = []
    for fact in case.facts:
        if fact.duration is None or fact.kind not in ("history", "diagnosis"):
            continue
        started = onset(fact.duration, case.admission_date)
        if started is None or started >= case.inception_date:
            continue
        declared = ", ".join(c.code for c in case.declared_peds) or "none"
        citations = [Citation(page=fact.source)] if fact.source else []
        findings.append(
            Finding(
                kind="pre_existing",
                text=(
                    f"{fact.text!r} ({fact.duration}) started by {started}, before the policy "
                    f"inception on {case.inception_date}; declared pre-existing diseases: "
                    f"{declared}"
                ),
                severity="adverse",
                citations=[*citations, Citation(ref=f"{CLAUSE_PREFIX}{PED_CLAUSE}")],
                judged_by="code",
            )
        )
    return findings
