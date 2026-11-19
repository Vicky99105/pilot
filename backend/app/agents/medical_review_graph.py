"""Medical review graph (LangGraph).

rewrite_terms -> lookup -> choose_codes -> hospital_codes -> length_of_stay ->
pre_existing -> validate -> (choose_codes once more, or end)

The model rewrites diagnosis phrases into index terms and chooses codes from the lookup
results. Code runs every lookup and every check. The validator accepts a code only if a
lookup in this run returned it and it is billable, and a finding only if it has a valid
citation. Rejected items go back to the model once; what is still wrong is dropped with
the reason.
"""

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from app.agents.medical_case import (
    CaseFile,
    compare_hospital_codes,
    length_of_stay,
    pre_existing,
    valid_citations,
)
from app.agents.prompts import (
    CHOOSE_PURPOSE,
    REWRITE_PURPOSE,
    ChooseCodesReply,
    TermRewriteReply,
    choose_messages,
    rewrite_messages,
)
from app.agents.tools import IcdLookupLedger
from app.services.knowledge_base.contract import IcdTabularEntry, KnowledgeBase
from app.services.medical_review.contract import (
    DroppedItem,
    Finding,
    LookupCall,
    VerifiedCode,
)
from app.services.model_gateway.contract import ModelGateway, ModelRole

MAX_REVISIONS = 1
NOT_RETURNED = "not returned by any lookup in this run"
NO_CITATION = "no valid citation"


class MedicalGraphResult(TypedDict):
    verified_codes: list[VerifiedCode]
    findings: list[Finding]
    query_items: list[str]
    lookups: list[LookupCall]
    dropped: list[DroppedItem]
    revisions: int


class _State(TypedDict, total=False):
    case: CaseFile
    terms: list[str]
    candidates: list[IcdTabularEntry]
    proposal: ChooseCodesReply
    code_findings: dict[str, list[Finding]]  # per check node, so a re-run replaces its own
    feedback: list[str]
    revisions: int
    verified_codes: list[VerifiedCode]
    findings: list[Finding]
    query_items: list[str]
    dropped: list[DroppedItem]


async def run_medical_review(
    case: CaseFile, *, gateway: ModelGateway, kb: KnowledgeBase, claim_id: UUID
) -> MedicalGraphResult:
    ledger = IcdLookupLedger(kb)

    async def rewrite_terms(state: _State) -> dict[str, Any]:
        reply = await gateway.structured(
            ModelRole.REASONING,
            rewrite_messages(state["case"]),
            TermRewriteReply,
            claim_id=claim_id,
            purpose=REWRITE_PURPOSE,
        )
        terms = [t.strip() for item in reply.items for t in item.index_terms if t.strip()]
        return {"terms": list(dict.fromkeys(terms))}

    async def lookup(state: _State) -> dict[str, Any]:
        codes: list[str] = []
        for term in state["terms"]:
            hits = await ledger.search_icd_index(term)
            codes.extend(h.code for h in hits if h.code)
        candidates = []
        for code in dict.fromkeys(codes):
            entry = await ledger.get_icd_tabular(code)
            if entry is not None:
                candidates.append(entry)
        return {"candidates": candidates}

    async def choose_codes(state: _State) -> dict[str, Any]:
        reply = await gateway.structured(
            ModelRole.REASONING,
            choose_messages(state["case"], state["candidates"], state.get("feedback", [])),
            ChooseCodesReply,
            claim_id=claim_id,
            purpose=CHOOSE_PURPOSE,
        )
        return {"proposal": reply}

    def hospital_codes(state: _State) -> dict[str, Any]:
        traced = [c.code for c in state["proposal"].codes if _code_problem(c.code) is None]
        found = compare_hospital_codes(state["case"], traced)
        return {"code_findings": {**state.get("code_findings", {}), "hospital": found}}

    def stay(state: _State) -> dict[str, Any]:
        reason = state["proposal"].long_stay_reason
        cited = reason is not None and bool(
            valid_citations([c.to_citation() for c in reason.citations], state["case"], _seen())
        )
        found = length_of_stay(state["case"], cited)
        return {"code_findings": {**state.get("code_findings", {}), "stay": found}}

    def pre_existing_disease(state: _State) -> dict[str, Any]:
        found = pre_existing(state["case"])
        return {"code_findings": {**state.get("code_findings", {}), "ped": found}}

    def validate(state: _State) -> dict[str, Any]:
        case, proposal = state["case"], state["proposal"]
        revisions = state.get("revisions", 0)
        verified: list[VerifiedCode] = []
        findings: list[Finding] = []
        rejected: list[tuple[DroppedItem, bool]] = []  # (item, fixable by the model)

        for choice in proposal.codes:
            code = choice.code.strip().upper()
            citations = valid_citations([c.to_citation() for c in choice.citations], case, _seen())
            problem = _code_problem(code) or (None if citations else NO_CITATION)
            if problem:
                rejected.append((DroppedItem(item="code", detail=code, reason=problem), True))
                continue
            if any(v.code.code == code for v in verified):
                continue
            entry = ledger.tabular(code)
            call = ledger.returned_by(code)
            assert entry is not None and call is not None
            verified.append(
                VerifiedCode(
                    code=entry.code,
                    phrase=choice.phrase,
                    lookup_call_id=call.call_id,
                    citations=citations,
                    rationale=choice.rationale,
                )
            )

        for reply in proposal.findings:
            citations = valid_citations([c.to_citation() for c in reply.citations], case, _seen())
            if not citations:
                rejected.append(
                    (DroppedItem(item="finding", detail=reply.text, reason=NO_CITATION), True)
                )
                continue
            findings.append(
                Finding(
                    kind="clinical",
                    text=reply.text,
                    severity=reply.severity,
                    citations=citations,
                    judged_by="model",
                )
            )

        for found in state.get("code_findings", {}).values():
            for finding in found:
                citations = valid_citations(finding.citations, case, _seen())
                if citations:
                    findings.append(finding.model_copy(update={"citations": citations}))
                else:  # the model cannot fix a code-side finding: drop it now
                    rejected.append(
                        (
                            DroppedItem(item="finding", detail=finding.text, reason=NO_CITATION),
                            False,
                        )
                    )

        fixable = [item for item, can_fix in rejected if can_fix]
        if fixable and revisions < MAX_REVISIONS:
            feedback = [f"{i.item} {i.detail!r}: {i.reason}" for i in fixable]
            return {"feedback": feedback, "revisions": revisions + 1}
        return {
            "feedback": [],
            "revisions": revisions,
            "verified_codes": verified,
            "findings": findings,
            "query_items": [q.strip() for q in proposal.query_items if q.strip()],
            "dropped": [item for item, _ in rejected],
        }

    def after_validate(state: _State) -> str:
        return "choose_codes" if state.get("feedback") else END

    def _seen() -> set[str]:
        return {code for call in ledger.calls for code in call.returned_codes}

    def _code_problem(code: str) -> str | None:
        key = code.strip().upper()
        if ledger.returned_by(key) is None:
            return NOT_RETURNED
        entry = ledger.tabular(key)
        if entry is None:
            return "no tabular entry was looked up"
        if not entry.billable:
            return "not billable; choose a more specific code from the candidates"
        return None

    graph = StateGraph(_State)
    graph.add_node("rewrite_terms", rewrite_terms)
    graph.add_node("lookup", lookup)
    graph.add_node("choose_codes", choose_codes)
    graph.add_node("hospital_codes", hospital_codes)
    graph.add_node("length_of_stay", stay)
    graph.add_node("pre_existing", pre_existing_disease)
    graph.add_node("validate", validate)
    graph.add_edge(START, "rewrite_terms")
    graph.add_edge("rewrite_terms", "lookup")
    graph.add_edge("lookup", "choose_codes")
    graph.add_edge("choose_codes", "hospital_codes")
    graph.add_edge("hospital_codes", "length_of_stay")
    graph.add_edge("length_of_stay", "pre_existing")
    graph.add_edge("pre_existing", "validate")
    graph.add_conditional_edges("validate", after_validate, ["choose_codes", END])

    final = await graph.compile().ainvoke({"case": case})
    return MedicalGraphResult(
        verified_codes=final["verified_codes"],
        findings=final["findings"],
        query_items=final["query_items"],
        lookups=list(ledger.calls),
        dropped=final["dropped"],
        revisions=final["revisions"],
    )
