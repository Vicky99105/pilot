"""Medical review graphs (LangGraph), one per step.

Coding (step 1, before bill review):
rewrite_terms -> lookup -> code_procedures -> choose_codes -> validate_codes ->
(choose_codes once more, or end)

Review (step 2, after bill review mapped the package):
findings -> hospital_codes -> length_of_stay -> pre_existing -> validate ->
(findings once more, or end)

The model rewrites diagnosis phrases into index terms and chooses codes from the lookup
results; in the review it writes findings and picks query items from the case's document
options. Code runs every lookup and every check. The validator accepts a code only if a
lookup in this run returned it and it is billable, and a finding only if it has a valid
citation. Rejected items go back to the model once; what is still wrong is dropped with
the reason.

Procedures are coded in ICD-10-PCS by a constrained builder: at each of the seven positions
the reply schema is the list of values the PCS tables allow after the code built so far, so
every code it builds is valid. Code takes a position itself when only one value is allowed.
"""

from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from app.agents.medical_case import (
    CaseFact,
    CaseFile,
    Statement,
    compare_hospital_codes,
    length_of_stay,
    pre_existing,
    valid_citations,
)
from app.agents.prompts import (
    CHOOSE_PURPOSE,
    FINDINGS_PURPOSE,
    NO_PROCEDURE,
    PCS_PURPOSE,
    REWRITE_PURPOSE,
    ChooseCodesReply,
    FindingsReply,
    TermRewrite,
    TermRewriteReply,
    choose_messages,
    choose_schema,
    findings_messages,
    findings_schema,
    pcs_choice,
    pcs_step_messages,
    pcs_step_schema,
    rewrite_messages,
    to_citation,
)
from app.agents.tools import IcdLookupLedger, index_code
from app.services.knowledge_base.contract import (
    ICD10PCS,
    PCS_LENGTH,
    IcdTabularEntry,
    IndexName,
    KnowledgeBase,
    PcsOption,
)
from app.services.medical_review.contract import (
    Citation,
    DocumentRequest,
    DroppedItem,
    Finding,
    LookupCall,
    PcsStep,
    VerifiedCode,
    VerifiedProcedure,
)
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.shared.types import Code

MAX_REVISIONS = 1
MAX_PROCEDURES = 3
# A code the index gives that is not billable is opened up to this many billable codes
# below it, so the model can pick one.
MAX_BELOW = 20
NOT_RETURNED = "not returned by any lookup in this run"
NO_CITATION = "no valid citation"


class CodingResult(TypedDict):
    verified_codes: list[VerifiedCode]
    procedure_codes: list[VerifiedProcedure]
    lookups: list[LookupCall]
    dropped: list[DroppedItem]
    revisions: int


class ReviewResult(TypedDict):
    findings: list[Finding]
    query_items: list[DocumentRequest]
    dropped: list[DroppedItem]
    revisions: int


def _queries(item: TermRewrite) -> tuple[str, list[str], IndexName]:
    """The main term with every modifier, then with each modifier on its own."""
    main = " ".join(item.main_term.split())
    modifiers = [" ".join(m.split()) for m in item.modifiers if m.strip()]
    queries = [" ".join([main, *modifiers])]
    if len(modifiers) > 1:
        queries += [f"{main} {m}" for m in modifiers]
    return main, queries, item.index


class _CodingState(TypedDict, total=False):
    case: CaseFile
    terms: list[tuple[str, list[str], IndexName]]  # (main term, queries, index)
    procedure_codes: list[VerifiedProcedure]
    procedure_dropped: list[DroppedItem]
    candidates: list[IcdTabularEntry]
    proposal: ChooseCodesReply
    feedback: list[str]
    revisions: int
    verified_codes: list[VerifiedCode]
    dropped: list[DroppedItem]


async def run_medical_coding(
    case: CaseFile, *, gateway: ModelGateway, kb: KnowledgeBase, claim_id: UUID
) -> CodingResult:
    ledger = IcdLookupLedger(kb, as_of=case.admission_date)

    async def rewrite_terms(state: _CodingState) -> dict[str, Any]:
        reply = await gateway.structured(
            ModelRole.REASONING,
            rewrite_messages(state["case"]),
            TermRewriteReply,
            claim_id=claim_id,
            purpose=REWRITE_PURPOSE,
        )
        return {"terms": [_queries(item) for item in reply.items if item.main_term.strip()]}

    async def lookup(state: _CodingState) -> dict[str, Any]:
        codes: list[str] = []
        searched: set[tuple[str, IndexName]] = set()
        for main, queries, index in state["terms"]:
            found: list[str] = []
            for query in [*queries, main]:
                if (query.lower(), index) in searched:
                    continue
                if query == main and found:
                    break  # the main term alone only when nothing more specific matched
                searched.add((query.lower(), index))
                hits = await ledger.search_icd_index(query, index)
                found += [index_code(h.code) for h in hits if h.code]
            codes += found
        candidates: list[IcdTabularEntry] = []
        for code in dict.fromkeys(codes):
            entry = await ledger.get_icd_tabular(code)
            if entry is None:
                continue
            candidates.append(entry)
            if not entry.billable:
                candidates += await _billable_below(entry)
        unique_candidates = list({c.code.code: c for c in candidates}.values())
        return {"candidates": unique_candidates}

    async def _billable_below(entry: IcdTabularEntry) -> list[IcdTabularEntry]:
        """Billable codes below a code that is not billable, breadth first, up to MAX_BELOW.
        Each is a tabular lookup, so each is in the ledger."""
        found: list[IcdTabularEntry] = []
        queue = list(entry.children)
        while queue and len(found) < MAX_BELOW:
            child = await ledger.get_icd_tabular(queue.pop(0))
            if child is None:
                continue
            if child.billable:
                found.append(child)
            else:
                queue += child.children
        return found

    async def code_procedures(state: _CodingState) -> dict[str, Any]:
        coded: list[VerifiedProcedure] = []
        dropped: list[DroppedItem] = []
        phrases = list({p.text.strip().lower(): p for p in state["case"].procedures}.values())
        for procedure in phrases[:MAX_PROCEDURES]:
            outcome = await _build_pcs(procedure)
            if isinstance(outcome, VerifiedProcedure):
                coded.append(outcome)
            elif outcome is not None:
                dropped.append(DroppedItem(item="procedure", detail=procedure.text, reason=outcome))
        return {"procedure_codes": coded, "procedure_dropped": dropped}

    async def _build_pcs(procedure: CaseFact) -> VerifiedProcedure | str | None:
        """The procedure's PCS code, or why there is none (None when no PCS tables are
        loaded)."""
        documented = [f.text for f in case.facts if f.kind == "procedure"]
        steps: list[PcsStep] = []
        built: list[tuple[PcsOption, str]] = []
        prefix = ""
        while len(prefix) < PCS_LENGTH:
            options, call = await ledger.pcs_options(prefix)
            if not options:
                return None if not prefix else f"no PCS table continues {prefix}"
            first = not prefix
            if len(options) == 1 and not first:
                chosen, by = options[0], "code"
            else:
                reply = await gateway.structured(
                    ModelRole.REASONING,
                    pcs_step_messages(procedure.text, documented, built, options),
                    pcs_step_schema(options, may_stop=first),
                    claim_id=claim_id,
                    purpose=PCS_PURPOSE,
                )
                if reply.value == NO_PROCEDURE:
                    return "no procedure was performed"
                chosen = next(o for o in options if pcs_choice(o) == reply.value)
                by = "model"
            steps.append(
                PcsStep(
                    position=chosen.position,
                    prefix=prefix,
                    value=chosen.value,
                    label=chosen.label,
                    lookup_call_id=call.call_id,
                    chosen_by=by,
                )
            )
            built.append((chosen, by))
            prefix += chosen.value
        valid, check = await ledger.pcs_validate(prefix)
        if not valid:  # cannot happen when every step came from pcs_options
            return f"{prefix} failed pcs_validate"
        entry = await ledger.get_icd_tabular(prefix, system=ICD10PCS)
        return VerifiedProcedure(
            code=entry.code if entry else Code(system=ICD10PCS, version="", code=prefix),
            phrase=procedure.text,
            steps=steps,
            validate_call_id=check.call_id,
            citations=[Citation(page=procedure.source)] if procedure.source else [],
        )

    async def choose_codes(state: _CodingState) -> dict[str, Any]:
        reply = await gateway.structured(
            ModelRole.REASONING,
            choose_messages(state["case"], state["candidates"], state.get("feedback", [])),
            choose_schema(state["case"], state["candidates"]),
            claim_id=claim_id,
            purpose=CHOOSE_PURPOSE,
        )
        return {"proposal": reply}

    def validate_codes(state: _CodingState) -> dict[str, Any]:
        case, proposal = state["case"], state["proposal"]
        revisions = state.get("revisions", 0)
        verified: list[VerifiedCode] = []
        rejected: list[DroppedItem] = []
        for choice in proposal.codes:
            code = choice.code.strip().upper()
            citations = valid_citations([to_citation(c) for c in choice.citations], case, _seen())
            problem = _code_problem(code) or (None if citations else NO_CITATION)
            if problem:
                rejected.append(DroppedItem(item="code", detail=code, reason=problem))
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
        if rejected and revisions < MAX_REVISIONS:
            feedback = [f"{i.item} {i.detail!r}: {i.reason}" for i in rejected]
            return {"feedback": feedback, "revisions": revisions + 1}
        return {
            "feedback": [],
            "revisions": revisions,
            "verified_codes": verified,
            "dropped": [*state.get("procedure_dropped", []), *rejected],
        }

    def after_validate(state: _CodingState) -> str:
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

    graph = StateGraph(_CodingState)
    graph.add_node("rewrite_terms", rewrite_terms)
    graph.add_node("lookup", lookup)
    graph.add_node("code_procedures", code_procedures)
    graph.add_node("choose_codes", choose_codes)
    graph.add_node("validate_codes", validate_codes)
    graph.add_edge(START, "rewrite_terms")
    graph.add_edge("rewrite_terms", "lookup")
    graph.add_edge("lookup", "code_procedures")
    graph.add_edge("code_procedures", "choose_codes")
    graph.add_edge("choose_codes", "validate_codes")
    graph.add_conditional_edges("validate_codes", after_validate, ["choose_codes", END])

    final = await graph.compile().ainvoke({"case": case})
    return CodingResult(
        verified_codes=final["verified_codes"],
        procedure_codes=final.get("procedure_codes", []),
        lookups=list(ledger.calls),
        dropped=final["dropped"],
        revisions=final["revisions"],
    )


class _ReviewState(TypedDict, total=False):
    case: CaseFile
    proposal: FindingsReply
    code_findings: dict[str, list[Finding]]  # per check node, so a re-run replaces its own
    feedback: list[str]
    revisions: int
    findings: list[Finding]
    query_items: list[DocumentRequest]
    dropped: list[DroppedItem]


async def run_medical_review(
    case: CaseFile,
    *,
    gateway: ModelGateway,
    claim_id: UUID,
    looked_up: set[str],
    reread: list[Statement] | None = None,
) -> ReviewResult:
    """looked_up: every code the coding step's lookups returned, which a citation may name.
    reread: what the pre-existing re-read of the cited history pages found."""

    async def findings(state: _ReviewState) -> dict[str, Any]:
        reply = await gateway.structured(
            ModelRole.REASONING,
            findings_messages(state["case"], state.get("feedback", [])),
            findings_schema(state["case"]),
            claim_id=claim_id,
            purpose=FINDINGS_PURPOSE,
        )
        return {"proposal": reply}

    def hospital_codes(state: _ReviewState) -> dict[str, Any]:
        found = compare_hospital_codes(state["case"], state["case"].verified_codes)
        return {"code_findings": {**state.get("code_findings", {}), "hospital": found}}

    def stay(state: _ReviewState) -> dict[str, Any]:
        reason = state["proposal"].long_stay_reason
        cited = reason is not None and bool(
            valid_citations([to_citation(c) for c in reason.citations], state["case"], looked_up)
        )
        found = length_of_stay(state["case"], cited)
        return {"code_findings": {**state.get("code_findings", {}), "stay": found}}

    def pre_existing_disease(state: _ReviewState) -> dict[str, Any]:
        found = pre_existing(state["case"], reread)
        return {"code_findings": {**state.get("code_findings", {}), "ped": found}}

    def validate(state: _ReviewState) -> dict[str, Any]:
        case, proposal = state["case"], state["proposal"]
        revisions = state.get("revisions", 0)
        kept: list[Finding] = []
        rejected: list[tuple[DroppedItem, bool]] = []  # (item, fixable by the model)

        for reply in proposal.findings:
            citations = valid_citations([to_citation(c) for c in reply.citations], case, looked_up)
            if not citations:
                rejected.append(
                    (DroppedItem(item="finding", detail=reply.text, reason=NO_CITATION), True)
                )
                continue
            kept.append(
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
                citations = valid_citations(finding.citations, case, looked_up)
                if citations:
                    kept.append(finding.model_copy(update={"citations": citations}))
                else:  # the model cannot fix a code-side finding: drop it now
                    rejected.append(
                        (
                            DroppedItem(item="finding", detail=finding.text, reason=NO_CITATION),
                            False,
                        )
                    )

        requests: list[DocumentRequest] = []
        options = {d.label: d for d in case.documents}
        for item in proposal.query_items:
            option = options.get(item.document)
            if option is None:  # the reply schema already excludes these; the second check
                reason = f"{item.document} is not a document this claim may be asked for"
                rejected.append(
                    (DroppedItem(item="query_item", detail=item.text, reason=reason), False)
                )
                continue
            same = next((r for r in requests if r.doc_type == option.doc_type), None)
            if same is None:
                requests.append(DocumentRequest(doc_type=option.doc_type, text=item.text))
            elif item.text not in same.text:  # one request per document type
                same.text = f"{same.text}; {item.text}"

        fixable = [item for item, can_fix in rejected if can_fix]
        if fixable and revisions < MAX_REVISIONS:
            feedback = [f"{i.item} {i.detail!r}: {i.reason}" for i in fixable]
            return {"feedback": feedback, "revisions": revisions + 1}
        return {
            "feedback": [],
            "revisions": revisions,
            "findings": kept,
            "query_items": requests,
            "dropped": [item for item, _ in rejected],
        }

    def after_validate(state: _ReviewState) -> str:
        return "findings" if state.get("feedback") else END

    graph = StateGraph(_ReviewState)
    graph.add_node("findings", findings)
    graph.add_node("hospital_codes", hospital_codes)
    graph.add_node("length_of_stay", stay)
    graph.add_node("pre_existing", pre_existing_disease)
    graph.add_node("validate", validate)
    graph.add_edge(START, "findings")
    graph.add_edge("findings", "hospital_codes")
    graph.add_edge("hospital_codes", "length_of_stay")
    graph.add_edge("length_of_stay", "pre_existing")
    graph.add_edge("pre_existing", "validate")
    graph.add_conditional_edges("validate", after_validate, ["findings", END])

    final = await graph.compile().ainvoke({"case": case})
    return ReviewResult(
        findings=final["findings"],
        query_items=final["query_items"],
        dropped=final["dropped"],
        revisions=final["revisions"],
    )
