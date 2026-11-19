"""Prompts and reply schemas for the agents. Code, not the model, does every lookup,
comparison and amount."""

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.agents.medical_case import CaseFile
from app.services.adjudication.contract import Decision
from app.services.knowledge_base.contract import IcdTabularEntry, PolicyClause
from app.services.medical_review.contract import Citation, Severity
from app.services.model_gateway.contract import Message
from app.shared.types import PageRef

REWRITE_PURPOSE = "medical_rewrite_terms"
CHOOSE_PURPOSE = "medical_choose_codes"
DECIDE_PURPOSE = "adjudication_decide"

CITATION_RULES = (
    "Every code and every finding must cite evidence. A citation is either a page of the "
    'claim, written {"document_id": "<uuid>", "page": <n>} using only the pages listed, or '
    'a reference, written {"ref": "clause:<clause_ref>"} or {"ref": "icd:<code>"}.'
)


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


# --- Medical review ------------------------------------------------------------------------


class TermRewrite(BaseModel):
    phrase: str  # the diagnosis as written
    index_terms: list[str]  # main term first, then modifiers, e.g. "appendicitis acute"


class TermRewriteReply(BaseModel):
    items: list[TermRewrite]


class CitationReply(BaseModel):
    document_id: UUID | None = None
    page: int | None = None
    ref: str | None = None

    def to_citation(self) -> Citation:
        if self.document_id is not None and self.page is not None:
            return Citation(page=PageRef(document_id=self.document_id, page=self.page))
        return Citation(ref=self.ref)


class CodeChoice(BaseModel):
    code: str
    phrase: str
    citations: list[CitationReply] = []
    rationale: str = ""


class FindingReply(BaseModel):
    text: str
    severity: Severity
    citations: list[CitationReply] = []


class StayReason(BaseModel):
    text: str
    citations: list[CitationReply] = []


class ChooseCodesReply(BaseModel):
    codes: list[CodeChoice]
    findings: list[FindingReply] = []
    query_items: list[str] = []
    long_stay_reason: StayReason | None = None


def _case_view(case: CaseFile) -> dict[str, Any]:
    return {
        "diagnoses": [d.model_dump(mode="json") for d in case.diagnoses],
        "facts": [f.model_dump(mode="json") for f in case.facts],
        "hospital_codes": [c.code for c in case.hospital_codes],
        "admission_date": case.admission_date,
        "discharge_date": case.discharge_date,
        "pages_you_may_cite": [p.model_dump(mode="json") for p in case.known_pages],
        "clauses_you_may_cite": case.clause_refs,
    }


def rewrite_messages(case: CaseFile) -> list[Message]:
    phrases = [d.text for d in case.diagnoses]
    return [
        Message(
            role="system",
            content=(
                "You prepare ICD-10-CM Alphabetic Index searches for a medical coder. For each "
                "diagnosis phrase, give one to three index terms: the main term (the condition "
                "noun, such as 'appendicitis') followed by the modifiers written in the phrase, "
                "in American spelling (localized, not localised). Do not choose codes."
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
                "using ICD-10-CM. You may only choose a billable code from the candidate list, "
                "which came from index and tabular lookups; never write a code from memory. "
                "Then list findings a medical officer should see (severity: info, query or "
                "adverse), and query items for anything the documents do not show. If the stay "
                "is longer than 7 days, give long_stay_reason only when a page states one. "
                + CITATION_RULES
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
    query_items: list[str] = []


def decide_messages(
    inputs: dict[str, Any], clauses: list[PolicyClause], feedback: list[str]
) -> list[Message]:
    messages = [
        Message(
            role="system",
            content=(
                "You draft a claim decision for an approver: approve, partial, query or reject. "
                "Decide in this order: if an eligibility rule failed, reject; else if mandatory "
                "documents are missing or medical review raised query items, query; else if any "
                "bill line has a deduction, partial; else approve. Give reasons, each tied to a "
                "clause_ref, rule_id or bill line_id from the inputs. Give one reason for every "
                "bill line with a deduction, naming its line_id. A reject or partial must cite a "
                "clause. Never state or compute an amount: code copies it from the deduction "
                "sheet. For a query, list the query items."
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
