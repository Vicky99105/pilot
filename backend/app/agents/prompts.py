"""Prompts and reply schemas for the agents. Code, not the model, does every lookup,
comparison and amount.

Each call that picks from a known set gets a reply schema built for that call, with an enum of
the only values it may return (closed_schema). The provider constrains its output to the
schema and the gateway rejects anything outside it; the code validators stay as the second
line of defence."""

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.agents.medical_case import CaseFile, stay_days, stay_limit, stg_key, stg_refs
from app.services.adjudication.contract import Decision
from app.services.knowledge_base.contract import (
    IcdTabularEntry,
    IndexName,
    PcsOption,
    PolicyClause,
)
from app.services.medical_review.contract import CLAUSE_PREFIX, ICD_PREFIX, Citation, Severity
from app.services.model_gateway.contract import Message
from app.shared.closed_schema import closed_schema, list_of, one_of
from app.shared.types import PageRef

REWRITE_PURPOSE = "medical_rewrite_terms"
CHOOSE_PURPOSE = "medical_choose_codes"
PCS_PURPOSE = "medical_pcs_step"
FINDINGS_PURPOSE = "medical_findings"
REREAD_PURPOSE = "medical_ped_reread"
NO_PROCEDURE = "none: no procedure was performed"
DECIDE_PURPOSE = "adjudication_decide"
PAGE_PREFIX = "page:"


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


# --- Medical review ------------------------------------------------------------------------


def page_key(page: PageRef) -> str:
    return f"{PAGE_PREFIX}{page.document_id}:{page.page}"


def to_citation(key: str) -> Citation:
    """A citation key ("page:<document_id>:<n>", "clause:<ref>" or "icd:<code>") as a
    Citation. Code checks it against the case afterwards."""
    if key.startswith(PAGE_PREFIX):
        document_id, _, number = key.removeprefix(PAGE_PREFIX).rpartition(":")
        return Citation(page=PageRef(document_id=UUID(document_id), page=int(number)))
    return Citation(ref=key)


def citation_keys(case: CaseFile, codes: list[str]) -> list[str]:
    """Everything a medical call may cite: the claim's pages, the policy clauses, the codes
    looked up or verified, and the sections of the package's guideline."""
    keys = [page_key(p) for p in case.known_pages]
    keys += [f"{CLAUSE_PREFIX}{ref}" for ref in case.clause_refs]
    keys += [f"{ICD_PREFIX}{code}" for code in codes]
    keys += stg_refs(case)
    return list(dict.fromkeys(keys))


class TermRewrite(BaseModel):
    """One diagnosis as the Alphabetic Index reads it. Code builds the searches: the main
    term with all modifiers, then with each modifier, then alone if nothing matched."""

    phrase: str  # the diagnosis as written
    main_term: str  # the condition noun, e.g. "appendicitis"
    modifiers: list[str] = []  # the words that qualify it, e.g. ["acute", "suppurative"]
    index: IndexName = "diseases"  # the index or table the terms are looked up in


class TermRewriteReply(BaseModel):
    items: list[TermRewrite]


class CodeChoice(BaseModel):
    code: str
    phrase: str
    citations: list[str] = []
    rationale: str = ""


class FindingReply(BaseModel):
    text: str
    severity: Severity
    citations: list[str] = []


class StayReason(BaseModel):
    text: str
    citations: list[str] = []


class ChooseCodesReply(BaseModel):
    codes: list[CodeChoice]


class FindingsReply(BaseModel):
    findings: list[FindingReply] = []
    query_items: list[str] = []  # labels of the case's document options
    long_stay_reason: StayReason | None = None


def choose_schema(case: CaseFile, candidates: list[IcdTabularEntry]) -> type[ChooseCodesReply]:
    """The choose reply for this case: codes are the billable codes the lookups returned;
    citations are the case's pages, clauses and looked-up codes."""
    billable = [c.code.code for c in candidates if c.billable]
    cites = list_of(citation_keys(case, [c.code.code for c in candidates]))
    code = closed_schema(
        CodeChoice,
        "CodeChoiceClosed",
        code=(one_of(billable), ...) if billable else (str, ...),
        citations=(cites, []),
    )
    return closed_schema(
        ChooseCodesReply,
        "ChooseCodesReply",
        codes=(list[code] if billable else list_of([]), ...),  # type: ignore[valid-type]
    )


def findings_schema(case: CaseFile) -> type[FindingsReply]:
    """The findings reply for this case: citations are the case's pages, clauses, verified
    codes and guideline sections; query items are labels of the case's document options and
    nothing else (an empty list when there are none); a long-stay reason only when the stay
    is over its norm."""
    cites = list_of(citation_keys(case, case.verified_codes))
    finding = closed_schema(FindingReply, "FindingClosed", citations=(cites, []))
    days, (limit, _) = stay_days(case), stay_limit(case)
    long_stay = days is not None and days > limit
    stay = closed_schema(StayReason, "StayReasonClosed", citations=(cites, []))
    return closed_schema(
        FindingsReply,
        "FindingsReply",
        findings=(list[finding], []),  # type: ignore[valid-type]
        query_items=(list_of([d.label for d in case.documents]), []),
        long_stay_reason=(stay | None, None) if long_stay else (None, None),
    )


def _case_view(case: CaseFile) -> dict[str, Any]:
    def fact(f: Any) -> dict[str, Any]:
        view = f.model_dump(mode="json", exclude={"source"}, exclude_none=True)
        return {**view, "page": page_key(f.source)} if f.source else view

    return {
        "diagnoses": [fact(d) for d in case.diagnoses],
        "facts": [fact(f) for f in case.facts],
        "hospital_codes": [c.code for c in case.hospital_codes],
        "admission_date": case.admission_date,
        "discharge_date": case.discharge_date,
    }


def rewrite_messages(case: CaseFile) -> list[Message]:
    phrases = [d.text for d in case.diagnoses]
    return [
        Message(
            role="system",
            content=(
                "You prepare ICD-10-CM Alphabetic Index searches for a medical coder. For each "
                "diagnosis phrase, give the main term (the condition noun, such as "
                "'appendicitis') and the modifiers written in the phrase, in American spelling "
                "(localized, not localised). Set index to the drug, "
                "neoplasm or external cause table when the phrase is a poisoning or drug "
                "effect, a neoplasm, or the cause of an injury."
            ),
        ),
        Message(role="user", content=f"Diagnosis phrases:\n{_json(phrases)}"),
    ]


def choose_messages(
    case: CaseFile, candidates: list[IcdTabularEntry], feedback: list[str]
) -> list[Message]:
    options = [
        {
            "code": c.code.code,
            "title": c.code.display,
            "billable": c.billable,
            "includes": c.includes,
            "excludes1": c.excludes1,
            "excludes2": c.excludes2,
            "code_first": c.code_first,
            "use_additional": c.use_additional,
        }
        for c in candidates
    ]
    messages = [
        Message(
            role="system",
            content=(
                "You are a medical coder for an Indian health insurer. Code each diagnosis "
                "in ICD-10-CM from the candidates, which came from index and tabular lookups, "
                "citing the pages that support each code."
            ),
        ),
        Message(
            role="user",
            content=f"Case file:\n{_json(_case_view(case))}\n\nCandidates:\n{_json(options)}",
        ),
    ]
    if feedback:
        messages.append(
            Message(
                role="user",
                content=(
                    "Your previous answer was rejected for these reasons. Answer again in full, "
                    "fixing them; anything still wrong will be dropped:\n- " + "\n- ".join(feedback)
                ),
            )
        )
    return messages


def findings_messages(case: CaseFile, feedback: list[str]) -> list[Message]:
    days, (limit, which) = stay_days(case), stay_limit(case)
    view = {
        **_case_view(case),
        "verified_codes": case.verified_codes,
        "guideline": {
            "code": case.guideline_code,
            "sections": {
                stg_key(case.guideline_code, section): items
                for section, items in case.guideline.items()
            }
            if case.guideline_code
            else {},
        },
        "stay": {"days": days, "norm_days": limit, "norm": which},
        "documents_that_may_be_requested": [
            {"document": d.label, "mandatory_in_guideline": d.from_guideline}
            for d in case.documents
        ],
    }
    messages = [
        Message(
            role="system",
            content=(
                "You are a medical reviewer for an Indian health insurer. Check the case "
                "against the package's treatment guideline. List findings a medical officer "
                "should see (severity: info, query or adverse), each citing its evidence. List "
                "as query items the documents the case needs. Give long_stay_reason when a page "
                "states why the stay was longer than its norm."
            ),
        ),
        Message(role="user", content=f"Case:\n{_json(view)}"),
    ]
    if feedback:
        messages.append(
            Message(
                role="user",
                content=(
                    "Your previous answer was rejected for these reasons. Answer again in full, "
                    "fixing them; anything still wrong will be dropped:\n- " + "\n- ".join(feedback)
                ),
            )
        )
    return messages


# --- Medical review: pre-existing disease re-read -----------------------------------------


class OnsetStatement(BaseModel):
    condition: str  # as written
    onset: str  # the onset or duration as written, e.g. "since 5 years", "in 2019"
    page: str  # one of the page keys read


class RereadReply(BaseModel):
    statements: list[OnsetStatement] = []


def reread_schema(pages: list[PageRef]) -> type[RereadReply]:
    statement = closed_schema(
        OnsetStatement, "OnsetStatementClosed", page=(one_of([page_key(p) for p in pages]), ...)
    )
    return closed_schema(RereadReply, "RereadReply", statements=(list[statement], []))  # type: ignore[valid-type]


def reread_prompt(pages: list[PageRef]) -> str:
    keys = ", ".join(f"image {i}: {page_key(p)}" for i, p in enumerate(pages, start=1))
    return (
        "These pages are from a hospital record. List each condition the patient's history "
        "says they had before this admission, with its onset or duration exactly as written "
        "(for example 'since 5 years' or 'diagnosed in 2019'), and the page it is on. "
        f"The pages, in order: {keys}."
    )


# --- Medical review: ICD-10-PCS builder ---------------------------------------------------


class PcsStepReply(BaseModel):
    value: str  # "<character>: <label>", one of the options


def pcs_choice(option: PcsOption) -> str:
    return f"{option.value}: {option.label}"


def pcs_step_schema(options: list[PcsOption], *, may_stop: bool) -> type[PcsStepReply]:
    """The reply for one step: exactly the valid values for the next character, and the
    stop value only when the step may say that no procedure was performed."""
    values = [pcs_choice(o) for o in options] + ([NO_PROCEDURE] if may_stop else [])
    return closed_schema(PcsStepReply, "PcsStepReply", value=(one_of(values), ...))


def pcs_step_messages(
    phrase: str,
    facts: list[str],
    built: list[tuple[PcsOption, str]],
    options: list[PcsOption],
) -> list[Message]:
    """built: the options already taken, each with how it was chosen."""
    nxt = options[0]
    view = {
        "procedure": phrase,
        "documented": facts,
        "code_so_far": [
            {"position": o.position, "axis": o.axis, "value": pcs_choice(o)} for o, _ in built
        ],
        "next": {"position": nxt.position, "axis": nxt.axis},
        "options": [
            {
                "value": pcs_choice(o),
                **({"definition": o.definition} if o.definition else {}),
                **({"explanation": o.explanation} if o.explanation else {}),
                **({"includes": o.includes} if o.includes else {}),
            }
            for o in options
        ],
    }
    return [
        Message(
            role="system",
            content=(
                "You build an ICD-10-PCS code for one procedure, one character at a time. "
                "Choose the option for the next character that the documented procedure "
                "supports."
            ),
        ),
        Message(role="user", content=_json(view)),
    ]


# --- Adjudication ---------------------------------------------------------------------------


class ReasonReply(BaseModel):
    text: str
    clause_ref: str | None = None
    rule_id: str | None = None
    line_id: int | None = None


class DecisionReply(BaseModel):
    decision: Decision
    reasons: list[ReasonReply]
    summary: str
    query_items: list[str] = []  # document types


def decide_schema(
    decision: Decision,
    clause_refs: list[str],
    rule_ids: list[str],
    line_ids: list[int],
    requestable: list[str],
) -> type[DecisionReply]:
    """The decision reply for these inputs: the decision code reached, reasons that can only
    name the clauses, rules and bill lines in the inputs, and query items that can only name
    the requested document types (none unless the decision is a query)."""

    def optional(values: Sequence[str | int]) -> tuple[Any, None]:
        return (one_of(values) | None, None) if values else (None, None)

    reason = closed_schema(
        ReasonReply,
        "ReasonClosed",
        clause_ref=optional(clause_refs),
        rule_id=optional(rule_ids),
        line_id=optional(line_ids),
    )
    asked = requestable if decision == "query" else []
    return closed_schema(
        DecisionReply,
        "DecisionReply",
        decision=(one_of([decision]), ...),
        reasons=(list[reason], ...),  # type: ignore[valid-type]
        query_items=(list_of(asked), []),
    )


def decide_messages(
    inputs: dict[str, Any], clauses: list[PolicyClause], feedback: list[str]
) -> list[Message]:
    messages = [
        Message(
            role="system",
            content=(
                "You write the reasons for a claim decision for an approver. The decision is "
                "set by the rules in code. Give reasons, each tied to a clause_ref, rule_id or "
                "bill line_id, and one reason for every bill line with a deduction. One reason "
                "with the rule_id and no line_id covers every line a claim-level rule (R16, "
                "R17, R19, R20) spread its deduction over. A reject or partial cites at least "
                "one clause. For a query, list the documents to ask for."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Inputs:\n{_json(inputs)}\n\nClauses:\n{_json([c.model_dump() for c in clauses])}"
            ),
        ),
    ]
    if feedback:
        messages.append(
            Message(
                role="user",
                content=(
                    "Your draft failed the self-check for these reasons. Answer again in full, "
                    "fixing them:\n- " + "\n- ".join(feedback)
                ),
            )
        )
    return messages
