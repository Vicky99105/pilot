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
    PED_CLAUSE,
    STG_PREFIX,
    Citation,
    DocumentRequest,
    Finding,
)
from app.shared.types import Code, DocType, PageRef

LONG_STAY_DAYS = 7  # with no norm for the package, longer stays need a stated reason
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


class DocumentOption(BaseModel):
    """A document a query may ask for: a mandatory document of the package's guideline that
    the claim does not hold, or, with no guideline, a medical document type. label is the
    value the reply schema allows and the text the claimant reads; doc_type is the type it
    asks for."""

    label: str
    doc_type: DocType
    from_guideline: bool = False


class CaseFile(BaseModel):
    """Everything the medical review graphs may read."""

    diagnoses: list[CaseFact]  # each diagnosis phrase as written, with its page
    procedures: list[CaseFact] = []  # each procedure phrase as written, with its page
    facts: list[CaseFact]  # every page-tagged clinical fact
    hospital_codes: list[Code]  # as the hospital wrote them; unverified
    admission_date: date | None
    discharge_date: date | None
    date_sources: list[PageRef]  # pages the stay dates came from
    inception_date: date
    declared_peds: list[Code]
    known_pages: list[PageRef]  # the pages a citation may name
    clause_refs: list[str]  # the policy clauses a citation may name
    # Documents a query may ask for: ones the claim does not hold and the claimant has not
    # already been asked for and answered.
    documents: list[DocumentOption] = []
    # The full review (step 2) only: the codes verified in step 1, which a citation may
    # name; the package's guideline, its sections citable as "stg:<code>:<section>"; and
    # the length-of-stay norm, when one is known.
    verified_codes: list[str] = []
    guideline_code: str | None = None
    guideline: dict[str, list[str]] = {}  # section -> items
    los_norm_days: int | None = None
    los_norm_source: str | None = None


def document_requests(options: list[DocumentOption]) -> list[DocumentRequest]:
    """The requests for the chosen options: one per document type, in the order chosen. The
    text is the chosen options' labels, so code writes every word the claimant reads."""
    labels: dict[DocType, list[str]] = {}
    for option in options:
        chosen = labels.setdefault(option.doc_type, [])
        if option.label not in chosen:
            chosen.append(option.label)
    return [DocumentRequest(doc_type=t, text="; ".join(names)) for t, names in labels.items()]


def documents_section(guideline: dict[str, list[str]]) -> str:
    """The guideline section that lists the mandatory documents: the pre-auth documents
    when the review read them (a cashless request before discharge), else the claim
    documents, else the claim checks."""
    for section in ("preauth_documents", "claim_documents"):
        if guideline.get(section):
            return section
    return "claim_checks"


def model_sections(case: "CaseFile") -> dict[str, list[str]]:
    """The guideline sections a model call may read and cite: every section except the
    mandatory document list, whose presence checks code makes."""
    hidden = documents_section(case.guideline)
    return {name: items for name, items in case.guideline.items() if name != hidden}


def stg_key(code: str, section: str) -> str:
    return f"{STG_PREFIX}{code}:{section}"


def stg_refs(case: CaseFile) -> list[str]:
    if case.guideline_code is None:
        return []
    return [stg_key(case.guideline_code, section) for section in case.guideline]


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
        known = looked_up | set(case.verified_codes)
        return None if code in known else f"{code} was not looked up"
    if ref.startswith(STG_PREFIX):
        return None if ref in stg_refs(case) else f"no guideline section {ref!r} for the package"
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


def stay_days(case: CaseFile) -> int | None:
    if case.admission_date is None or case.discharge_date is None:
        return None
    return (case.discharge_date - case.admission_date).days


def stay_limit(case: CaseFile) -> tuple[int, str]:
    """The days a stay may last without a stated reason: the package's norm when known,
    else LONG_STAY_DAYS."""
    if case.los_norm_days is not None:
        return case.los_norm_days, f"the {case.los_norm_source or 'package'} norm"
    return LONG_STAY_DAYS, "the general limit"


def length_of_stay(case: CaseFile, reason_cited: bool) -> list[Finding]:
    """A stay over its norm with no cited reason is flagged for information."""
    days = stay_days(case)
    limit, which = stay_limit(case)
    if days is None or days <= limit or reason_cited:
        return []
    citations = [Citation(page=p) for p in unique(case.date_sources)] or _first_pages(case)
    if case.los_norm_days is not None and case.guideline_code is not None:
        alos = stg_key(case.guideline_code, "alos")
        if alos in stg_refs(case):
            citations.append(Citation(ref=alos))
    return [
        Finding(
            kind="length_of_stay",
            text=(
                f"stay of {days} days ({case.admission_date} to {case.discharge_date}) is over "
                f"{which} of {limit} days and no reason is stated"
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


class Statement(BaseModel):
    """A condition with its onset as written, from a fact or from the re-read of a page."""

    condition: str
    onset: str
    page: PageRef | None


def history_pages(case: CaseFile) -> list[PageRef]:
    """The pages the history facts cite: the only pages the pre-existing re-read reads."""
    return unique([f.source for f in case.facts if f.kind == "history" and f.source])


def pre_existing_onsets(
    case: CaseFile, reread: list[Statement] | None = None
) -> list[tuple[Statement, date]]:
    """Conditions whose stated onset puts their start before the policy inception, with that
    start: from the facts' durations, then from the re-read of the cited history pages."""
    if case.admission_date is None:
        return []
    statements = [
        Statement(condition=f.text, onset=f.duration, page=f.source)
        for f in case.facts
        if f.duration is not None and f.kind in ("history", "diagnosis")
    ]
    found: list[tuple[Statement, date]] = []
    seen: set[tuple[date, str]] = set()
    for statement in [*statements, *(reread or [])]:
        started = onset(statement.onset, case.admission_date)
        if started is None or started >= case.inception_date:
            continue
        key = (started, statement.condition.strip().lower())
        if key not in seen:
            seen.add(key)
            found.append((statement, started))
    return found


def pre_existing(case: CaseFile, reread: list[Statement] | None = None) -> list[Finding]:
    """One adverse finding for each condition that started before the policy inception."""
    declared = ", ".join(c.code for c in case.declared_peds) or "none"
    findings = []
    for statement, started in pre_existing_onsets(case, reread):
        citations = [Citation(page=statement.page)] if statement.page else []
        findings.append(
            Finding(
                kind="pre_existing",
                text=(
                    f"{statement.condition!r} ({statement.onset}) started by {started}, before "
                    f"the policy inception on {case.inception_date}; declared pre-existing "
                    f"diseases: {declared}"
                ),
                severity="adverse",
                citations=[*citations, Citation(ref=f"{CLAUSE_PREFIX}{PED_CLAUSE}")],
                judged_by="code",
            )
        )
    return findings
