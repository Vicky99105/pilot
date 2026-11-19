"""Adjudication graph (LangGraph).

read_inputs -> decide -> self_check -> (decide once more, or) -> draft -> guardrail

The model chooses the decision and writes reasons tied to clauses, rules or bill lines.
Code checks the reasons, copies the amount from the deduction sheet and, in the guardrail,
re-reads the sheet and the balance through the tools.
"""

from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from app.agents.prompts import DECIDE_PURPOSE, DecisionReply, decide_messages
from app.agents.tools import AdjudicationTools
from app.services.adjudication.contract import (
    Decision,
    DraftDecision,
    NotReadyForAdjudication,
    Reason,
)
from app.services.bill_review.contract import DeductionSheet
from app.services.knowledge_base.contract import PolicyClause
from app.services.medical_review.contract import CLAUSE_PREFIX, MedicalFindings
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.registration.contract import RegistrationResult
from app.services.risk.contract import RiskReport
from app.shared.types import RuleResult, Stage

MAX_REVISIONS = 1
ZERO = Decimal("0.00")
PAID: tuple[Decision, ...] = ("approve", "partial")


class AdjudicationInputs(BaseModel):
    registration: RegistrationResult
    sheet: DeductionSheet
    medical: MedicalFindings | None
    risk: RiskReport | None
    outstanding_documents: list[str]
    versions: dict[str, int | None]
    failed_rules: list[RuleResult]
    clauses: list[PolicyClause]
    expected: Decision
    expected_because: str


class _State(TypedDict, total=False):
    inputs: AdjudicationInputs
    reply: DecisionReply
    feedback: list[str]
    self_check_errors: list[str]
    revisions: int
    draft: DraftDecision


def expected_decision(
    failed_rules: list[RuleResult],
    outstanding: list[str],
    medical: MedicalFindings | None,
    sheet: DeductionSheet,
) -> tuple[Decision, str]:
    """The decision rules, in order: failed eligibility, then missing information, then
    deductions."""
    if failed_rules:
        return "reject", "eligibility rules failed: " + ", ".join(r.rule_id for r in failed_rules)
    query_items = medical.query_items if medical else []
    if outstanding or query_items:
        missing = [*outstanding, *query_items]
        return "query", "information is missing: " + ", ".join(missing)
    deducted = [ln.line_id for ln in sheet.lines if ln.deducted > 0]
    if deducted:
        return "partial", "bill lines have deductions: " + ", ".join(map(str, deducted))
    return "approve", "every check passed and nothing was deducted"


async def run_adjudication(
    claim_id: UUID, *, gateway: ModelGateway, tools: AdjudicationTools
) -> DraftDecision:
    async def read_inputs(state: _State) -> dict[str, Any]:
        registered = await tools.get_stage_result(Stage.REGISTRATION)
        reviewed = await tools.get_stage_result(Stage.BILL_REVIEW)
        if registered is None or reviewed is None:
            raise NotReadyForAdjudication(f"claim {claim_id} has no registration or sheet")
        registration = RegistrationResult.model_validate(registered.payload)
        if registration.policy is None:
            raise NotReadyForAdjudication(f"claim {claim_id} has no policy")
        sheet = DeductionSheet.model_validate(reviewed.payload)
        medical_snap = await tools.get_stage_result(Stage.MEDICAL_REVIEW)
        risk_snap = await tools.get_stage_result(Stage.RISK)
        intake = await tools.get_stage_result(Stage.INTAKE)
        classified = await tools.get_stage_result(Stage.CLASSIFICATION)
        medical = MedicalFindings.model_validate(medical_snap.payload) if medical_snap else None
        risk = RiskReport.model_validate(risk_snap.payload) if risk_snap else None

        received = {
            d.get("doc_type")
            for d in (classified.payload.get("documents", []) if classified else [])
        }
        missing = intake.payload.get("missing_documents", []) if intake else []
        outstanding = [d for d in missing if d not in received]
        failed = [r for r in registration.rule_results if not r.passed]
        failed += [r for r in (medical.rule_results if medical else []) if not r.passed]

        refs = [r.clause_ref for r in failed]
        refs += [ln.clause_ref for ln in sheet.lines if ln.deducted > 0]
        refs += [r.clause_ref for r in sheet.rule_results if r.rule_id == "R21"]
        for finding in medical.findings if medical else []:
            refs += [
                c.ref.removeprefix(CLAUSE_PREFIX)
                for c in finding.citations
                if c.ref and c.ref.startswith(CLAUSE_PREFIX)
            ]
        clauses = []
        for ref in dict.fromkeys(r for r in refs if r):
            clause = await tools.get_clause(ref)
            if clause is not None:
                clauses.append(clause)

        expected, because = expected_decision(failed, outstanding, medical, sheet)
        versions = {
            "registration": registered.version,
            "bill_review": reviewed.version,
            "medical_review": medical_snap.version if medical_snap else None,
            "risk": risk_snap.version if risk_snap else None,
            "classification": classified.version if classified else None,
        }
        inputs = AdjudicationInputs(
            registration=registration,
            sheet=sheet,
            medical=medical,
            risk=risk,
            outstanding_documents=outstanding,
            versions=versions,
            failed_rules=failed,
            clauses=clauses,
            expected=expected,
            expected_because=because,
        )
        return {"inputs": inputs}

    async def decide(state: _State) -> dict[str, Any]:
        inputs = state["inputs"]
        reply = await gateway.structured(
            ModelRole.REASONING,
            decide_messages(_view(inputs), inputs.clauses, state.get("feedback", [])),
            DecisionReply,
            claim_id=claim_id,
            purpose=DECIDE_PURPOSE,
        )
        return {"reply": reply}

    def self_check(state: _State) -> dict[str, Any]:
        errors = _self_check(state["inputs"], state["reply"])
        revisions = state.get("revisions", 0)
        if errors and revisions < MAX_REVISIONS:
            return {"feedback": errors, "revisions": revisions + 1}
        return {"feedback": [], "self_check_errors": errors, "revisions": revisions}

    def after_check(state: _State) -> str:
        return "decide" if state.get("feedback") else "draft"

    def draft(state: _State) -> dict[str, Any]:
        inputs, reply = state["inputs"], state["reply"]
        payable = inputs.sheet.payable
        assert inputs.registration.policy is not None
        return {
            "draft": DraftDecision(
                claim_id=claim_id,
                input_versions=inputs.versions,
                decision=reply.decision,
                expected_decision=inputs.expected,
                amount=payable if reply.decision in PAID else ZERO,
                payable_on_sheet=payable,
                available_balance=inputs.registration.policy.balance,
                reasons=[Reason(**r.model_dump()) for r in reply.reasons],
                query_items=[q.strip() for q in reply.query_items if q.strip()],
                summary=reply.summary.strip(),
                self_check_errors=state.get("self_check_errors", []),
                guardrail_errors=[],
                guardrail_passed=False,
                revisions=state.get("revisions", 0),
            )
        }

    async def guardrail(state: _State) -> dict[str, Any]:
        current = state["draft"]
        errors = []
        balance = ZERO
        reviewed = await tools.get_stage_result(Stage.BILL_REVIEW)
        registered = await tools.get_stage_result(Stage.REGISTRATION)
        if reviewed is None or registered is None:
            errors.append("the deduction sheet or registration is no longer readable")
        else:
            sheet = DeductionSheet.model_validate(reviewed.payload)
            policy = RegistrationResult.model_validate(registered.payload).policy
            balance = policy.balance if policy else ZERO
            if reviewed.version != current.input_versions["bill_review"]:
                errors.append(
                    f"the deduction sheet changed from version "
                    f"{current.input_versions['bill_review']} to {reviewed.version}"
                )
            if current.decision in PAID and current.amount != sheet.payable:
                errors.append(
                    f"amount {_rupees(current.amount)} differs from the sheet payable "
                    f"{_rupees(sheet.payable)}"
                )
            if current.decision not in PAID and current.amount != ZERO:
                errors.append(f"a {current.decision} must have amount 0")
            if current.amount > balance:
                errors.append(
                    f"amount {_rupees(current.amount)} exceeds the available balance "
                    f"{_rupees(balance)}"
                )
        passed = not errors and not current.self_check_errors
        return {
            "draft": current.model_copy(
                update={
                    "guardrail_errors": errors,
                    "guardrail_passed": passed,
                    "available_balance": balance,
                }
            )
        }

    graph = StateGraph(_State)
    graph.add_node("read_inputs", read_inputs)
    graph.add_node("decide", decide)
    graph.add_node("self_check", self_check)
    graph.add_node("draft", draft)
    graph.add_node("guardrail", guardrail)
    graph.add_edge(START, "read_inputs")
    graph.add_edge("read_inputs", "decide")
    graph.add_edge("decide", "self_check")
    graph.add_conditional_edges("self_check", after_check, ["decide", "draft"])
    graph.add_edge("draft", "guardrail")
    graph.add_edge("guardrail", END)

    final = await graph.compile().ainvoke({})
    result: DraftDecision = final["draft"]
    return result


def _rupees(value: Decimal) -> str:
    return f"Rs {value:,.2f}"


def _view(inputs: AdjudicationInputs) -> dict[str, Any]:
    """What the model sees. Amounts are shown for context only."""
    sheet = inputs.sheet
    medical = inputs.medical
    return {
        "failed_eligibility_rules": [r.model_dump(mode="json") for r in inputs.failed_rules],
        "outstanding_documents": inputs.outstanding_documents,
        "deductions": [
            {
                "line_id": ln.line_id,
                "description": ln.description,
                "claimed": str(ln.claimed),
                "deducted": str(ln.deducted),
                "rule_id": ln.rule_id,
                "clause_ref": ln.clause_ref,
                "reason": ln.reason,
            }
            for ln in sheet.lines
            if ln.deducted > 0
        ],
        "rule_results": [
            r.model_dump(mode="json")
            for r in [*inputs.registration.rule_results, *sheet.rule_results]
            if not r.passed or r.rule_id == "R21"
        ],
        "medical": {
            "verified_codes": [v.code.code for v in medical.verified_codes] if medical else [],
            "findings": [f.model_dump(mode="json") for f in medical.findings] if medical else [],
            "query_items": medical.query_items if medical else [],
        },
        "risk": {
            "score": inputs.risk.score if inputs.risk else None,
            "refer": inputs.risk.refer if inputs.risk else None,
            "explanation": inputs.risk.explanation if inputs.risk else None,
        },
        "payable_on_sheet": str(sheet.payable),
    }


def _self_check(inputs: AdjudicationInputs, reply: DecisionReply) -> list[str]:
    errors = []
    if reply.decision != inputs.expected:
        errors.append(
            f"decision {reply.decision} contradicts the inputs: expected {inputs.expected} "
            f"because {inputs.expected_because}"
        )
    clause_refs = {c.clause_ref for c in inputs.clauses}
    rule_ids = {r.rule_id for r in inputs.registration.rule_results}
    rule_ids |= {r.rule_id for r in inputs.sheet.rule_results}
    rule_ids |= {r.rule_id for r in (inputs.medical.rule_results if inputs.medical else [])}
    line_ids = {ln.line_id for ln in inputs.sheet.lines}
    for i, reason in enumerate(reply.reasons, start=1):
        if not (reason.clause_ref or reason.rule_id or reason.line_id is not None):
            errors.append(f"reason {i} cites no clause, rule or bill line")
        if reason.clause_ref and reason.clause_ref not in clause_refs:
            errors.append(
                f"reason {i} cites clause {reason.clause_ref}, which is not in the inputs"
            )
        if reason.rule_id and reason.rule_id not in rule_ids:
            errors.append(f"reason {i} cites rule {reason.rule_id}, which did not run")
        if reason.line_id is not None and reason.line_id not in line_ids:
            errors.append(f"reason {i} cites bill line {reason.line_id}, which is not on the sheet")
    if reply.decision == "partial":
        explained = {r.line_id for r in reply.reasons if r.line_id is not None}
        for line in inputs.sheet.lines:
            if line.deducted > 0 and line.line_id not in explained:
                errors.append(f"bill line {line.line_id} has a deduction but no reason")
    if reply.decision in ("partial", "reject") and not any(r.clause_ref for r in reply.reasons):
        errors.append(f"a {reply.decision} must cite at least one clause")
    if reply.decision == "query" and not any(q.strip() for q in reply.query_items):
        errors.append("a query must list what to ask for")
    return errors
