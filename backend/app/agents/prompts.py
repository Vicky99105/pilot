"""Prompts and reply schemas for the agents. Code, not the model, does every lookup,
comparison and amount.

Each call that picks from a known set gets a reply schema built for that call, with an enum of
the only values it may return (closed_schema). The provider constrains its output to the
schema and the gateway rejects anything outside it; the code validators stay as the second
line of defence."""

import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, WithJsonSchema, create_model

from app.agents.medical_case import CaseFile
from app.services.adjudication.contract import Decision
from app.services.knowledge_base.contract import (
    IcdTabularEntry,
    IndexName,
    PcsOption,
    PolicyClause,
)
from app.services.medical_review.contract import CLAUSE_PREFIX, ICD_PREFIX, Citation, Severity
from app.services.model_gateway.contract import Message
from app.shared.types import PageRef

REWRITE_PURPOSE = "medical_rewrite_terms"
CHOOSE_PURPOSE = "medical_choose_codes"
PCS_PURPOSE = "medical_pcs_step"
NO_PROCEDURE = "none: no procedure was performed"
DECIDE_PURPOSE = "adjudication_decide"
PAGE_PREFIX = "page:"

ModelT = TypeVar("ModelT", bound=BaseModel)


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


# --- Closed answer spaces --------------------------------------------------------------------


def one_of(values: Sequence[str | int]) -> Any:
    """A type that accepts exactly these values. Its JSON schema is always an enum (pydantic
    would write a single value as const)."""
    kind = "integer" if all(isinstance(v, int) for v in values) else "string"
    allowed = Literal.__getitem__(tuple(values))  # Literal["a", "b"], built at run time
    return Annotated[allowed, WithJsonSchema({"type": kind, "enum": list(values)})]


def list_of(values: Sequence[str | int]) -> Any:
    """A list of the given values; a list that must stay empty when there are none."""
    if not values:
        return Annotated[list[str], Field(max_length=0)]
    return list.__class_getitem__(one_of(values))  # list[<the enum>], built at run time


def closed_schema(base: type[ModelT], name: str, **fields: Any) -> type[ModelT]:
    """A subclass of base whose named fields are replaced, for one call."""
    return create_model(name, __base__=base, **fields)


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


def citation_keys(case: CaseFile, candidates: list[IcdTabularEntry]) -> list[str]:
    """Everything the choose call may cite: the claim's pages, the policy clauses and the
    codes the lookups returned."""
    keys = [page_key(p) for p in case.known_pages]
    keys += [f"{CLAUSE_PREFIX}{ref}" for ref in case.clause_refs]
    keys += [f"{ICD_PREFIX}{c.code.code}" for c in candidates]
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


class DocumentRequestReply(BaseModel):
    doc_type: str
    text: str  # the request as the claimant reads it


class ChooseCodesReply(BaseModel):
    codes: list[CodeChoice]
    findings: list[FindingReply] = []
    query_items: list[DocumentRequestReply] = []
    long_stay_reason: StayReason | None = None


def choose_schema(case: CaseFile, candidates: list[IcdTabularEntry]) -> type[ChooseCodesReply]:
    """The choose reply for this case: codes are the billable codes the lookups returned,
    citations are the case's pages, clauses and looked-up codes, and query items name only
    the document types the claim may still be asked for."""
    billable = [c.code.code for c in candidates if c.billable]
    cites = list_of(citation_keys(case, candidates))
    code = closed_schema(
        CodeChoice,
        "CodeChoiceClosed",
        code=(one_of(billable), ...) if billable else (str, ...),
        citations=(cites, []),
    )
    finding = closed_schema(FindingReply, "FindingClosed", citations=(cites, []))
    stay = closed_schema(StayReason, "StayReasonClosed", citations=(cites, []))
    requestable = [d.value for d in case.requestable]
    request = closed_schema(
        DocumentRequestReply,
        "DocumentRequestClosed",
        doc_type=(one_of(requestable), ...) if requestable else (str, ...),
    )
    return closed_schema(
        ChooseCodesReply,
        "ChooseCodesReply",
        codes=(list[code] if billable else list_of([]), ...),  # type: ignore[valid-type]
        findings=(list[finding], []),  # type: ignore[valid-type]
        query_items=(list[request] if requestable else list_of([]), []),  # type: ignore[valid-type]
        long_stay_reason=(stay | None, None),
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
                "You are a medical reviewer for an Indian health insurer. Code each diagnosis "
                "in ICD-10-CM from the candidates, which came from index and tabular lookups. "
                "List findings a medical officer should see (severity: info, query or adverse), "
                "each citing its evidence. List as query items the documents the case needs "
                "and does not contain. If the stay is longer than 7 days, give "
                "long_stay_reason when a page states one."
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
                "bill line_id, and one reason for every bill line with a deduction. A reject "
                "or partial cites at least one clause. For a query, list the documents to ask "
                "for."
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
