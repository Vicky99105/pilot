"""Adjudication through its contract (and the graph through its tools contract): the model
drafts, code self-checks, copies the amount and runs the guardrail."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.adjudication_graph import run_adjudication
from app.agents.tools import StageSnapshot
from app.config import Settings
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.adjudication.contract import (
    DOCUMENTS_CLAUSE,
    AdjudicationService,
    DraftDecision,
    NotReadyForAdjudication,
)
from app.services.adjudication.service import DefaultAdjudicationService
from app.services.bill_review.service import DefaultBillReviewService
from app.services.knowledge_base.contract import PolicyClause
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import (
    Citation,
    DocumentRequest,
    Finding,
    MedicalFindings,
)
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.contract import RiskReport
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import PRODUCT_UIN
from app.shared.types import DocType, RequestType, RuleResult, Stage
from tests.database.factories import add_claim
from tests.fakes.adjudication import FakeAdjudicationTools
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import (
    LAP_APPENDICECTOMY,
    ClaimSpec,
    add_coding,
    add_digitised_claim,
    reviewed_bill,
    without_agreements,
)
from tests.services.reference_rows import add_reference_rows
from tests.services.test_registration import asha_policy

LAP = (LAP_APPENDICECTOMY,)

D = Decimal
PAYABLE = D("133000.00")  # the worked example: admissible Rs 1,40,000 less 5% co-pay


def line_reason(line_id: int, clause: str) -> dict[str, Any]:
    return {
        "text": f"line {line_id} reduced",
        "clause_ref": clause,
        "rule_id": "R13",
        "line_id": line_id,
    }


ALL_LINES = [
    line_reason(1, "ROOM-CAP"),
    line_reason(2, "PROP-DED"),
    line_reason(3, "PROP-DED"),
    line_reason(4, "PROP-DED"),
]
PARTIAL = {"decision": "partial", "reasons": ALL_LINES, "summary": "Room cap breached."}


async def add_medical(
    sessions: async_sessionmaker[AsyncSession],
    claim_id: UUID,
    query_items: list[DocumentRequest] | None = None,
) -> None:
    medical = MedicalFindings(
        claim_id=claim_id,
        digitisation_version=1,
        registration_version=1,
        verified_codes=[],
        findings=[],
        query_items=query_items or [],
        rule_results=[],
        lookups=[],
        dropped=[],
        revisions=0,
        adverse=False,
    )
    await StageRecordRepository(sessions).append(
        claim_id, Stage.MEDICAL_REVIEW, medical.model_dump(mode="json"), created_by="test"
    )
    risk = RiskReport(
        claim_id=claim_id,
        registration_version=1,
        hits=[],
        score=0,
        refer=False,
        explanation="No red flags.",
    )
    await StageRecordRepository(sessions).append(
        claim_id, Stage.RISK, risk.model_dump(mode="json"), created_by="test"
    )


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    policies: FakePolicyService

    async def reviewed(self, query_items: list[DocumentRequest] | None = None) -> UUID:
        """A claim through registration, bill review, medical review and risk."""
        claim_id = await add_digitised_claim(self.sessions, ClaimSpec())
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        await DefaultRegistrationService(
            self.sessions, self.policies, FakeMoneyService(), rules
        ).register(claim_id)
        await without_agreements(self.sessions)
        await reviewed_bill(self.sessions, claim_id)
        await add_medical(self.sessions, claim_id, query_items)
        return claim_id

    def service(self, gateway: FakeModelGateway) -> AdjudicationService:
        return DefaultAdjudicationService(
            self.sessions, gateway, DefaultKnowledgeBase(self.sessions)
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions, FakePolicyService([asha_policy()]))


async def test_deductions_give_a_partial_with_the_sheet_payable_as_the_amount(env: Env) -> None:
    claim_id = await env.reviewed()
    missing_line_4 = {**PARTIAL, "reasons": ALL_LINES[:3]}
    gateway = FakeModelGateway({"adjudication_decide": [missing_line_4, PARTIAL]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.decision, draft.expected_decision) == ("partial", "partial")
    assert draft.amount == draft.payable_on_sheet == PAYABLE
    assert draft.guardrail_passed is True
    assert (draft.guardrail_errors, draft.self_check_errors) == ([], [])
    assert draft.revisions == 1  # line 4 had no reason the first time
    feedback = gateway.calls[-1].messages[-1].content  # type: ignore[index]
    assert "line 4" in feedback
    assert {r.line_id for r in draft.reasons} == {1, 2, 3, 4}
    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.ADJUDICATION)
    assert stored is not None and DraftDecision.model_validate(stored.payload) == draft


async def test_a_failed_eligibility_rule_gives_a_reject_with_zero_amount(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2026, 10, 20), continuous_cover_start=date(2026, 10, 20))
    ]
    claim_id = await env.reviewed()
    reject = {
        "decision": "reject",
        "reasons": [{"text": "Within the first 30 days", "clause_ref": "Excl03", "rule_id": "R5"}],
        "summary": "Illness began within the initial waiting period.",
    }
    gateway = FakeModelGateway({"adjudication_decide": [reject]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.decision, draft.expected_decision) == ("reject", "reject")
    assert draft.amount == D("0.00")
    assert draft.reasons[0].clause_ref == "Excl03"
    assert draft.guardrail_passed is True


CT_REPORT = DocumentRequest(doc_type=DocType.INVESTIGATION_REPORT, text="CT abdomen report")


def query_reply(*doc_types: str) -> dict[str, Any]:
    return {
        "decision": "query",
        "reasons": [{"text": "Imaging report needed", "rule_id": "R13", "line_id": 1}],
        "summary": "Asking for the CT report.",
        "query_items": list(doc_types),
    }


async def test_medical_query_items_give_a_query(env: Env) -> None:
    claim_id = await env.reviewed(query_items=[CT_REPORT])
    gateway = FakeModelGateway({"adjudication_decide": [query_reply("investigation_report")]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.decision, draft.amount, draft.query_items) == ("query", D("0.00"), [CT_REPORT])
    assert draft.guardrail_passed is True


async def test_a_query_may_only_ask_for_documents_the_inputs_request(env: Env) -> None:
    """The draft's query items are document types from the outstanding documents or the
    medical review's requests: the reply schema lists only those."""
    claim_id = await env.reviewed(query_items=[CT_REPORT])
    gateway = FakeModelGateway({"adjudication_decide": [query_reply("investigation_report")]})

    await env.service(gateway).draft(claim_id)

    schema = gateway.calls[-1].schema
    assert schema is not None
    items = schema.model_json_schema()["properties"]["query_items"]["items"]
    assert items["enum"] == ["investigation_report"]
    with pytest.raises(ValidationError):
        schema.model_validate(query_reply("investigation_report", "final_bill"))


async def test_the_reply_schema_allows_only_the_decision_and_ids_in_the_inputs(
    env: Env,
) -> None:
    """Code decides; the model's reply can only carry that decision, the rules that ran, the
    sheet's lines and the clauses read, and no query items unless the decision is a query."""
    claim_id = await env.reviewed()
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    await env.service(gateway).draft(claim_id)

    schema = gateway.calls[-1].schema
    assert schema is not None
    spec = schema.model_json_schema()
    assert spec["properties"]["decision"]["enum"] == ["partial"]
    assert spec["properties"]["query_items"]["maxItems"] == 0
    reason = spec["$defs"]["ReasonClosed"]["properties"]
    enums = {k: v["anyOf"][0]["enum"] for k, v in reason.items() if "anyOf" in v}
    assert set(enums["clause_ref"]) >= {"ROOM-CAP", "PROP-DED"}
    assert "R13" in enums["rule_id"] and "R99" not in enums["rule_id"]
    sheet = await StageRecordRepository(env.sessions).latest(claim_id, Stage.BILL_REVIEW)
    assert sheet is not None
    assert enums["line_id"] == [ln["line_id"] for ln in sheet.payload["lines"]]
    approve = {"decision": "approve", "reasons": ALL_LINES, "summary": "Looks fine."}
    with pytest.raises(ValidationError):
        schema.model_validate(approve)
    with pytest.raises(ValidationError):  # a bill line that is not on the sheet
        schema.model_validate({**PARTIAL, "reasons": [line_reason(9, "ROOM-CAP")]})


async def test_drafting_is_idempotent_per_input_versions(env: Env) -> None:
    claim_id = await env.reviewed()
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})
    service = env.service(gateway)

    assert await service.draft(claim_id) == await service.draft(claim_id)
    assert len(gateway.calls) == 1


async def test_a_claim_without_a_deduction_sheet_is_not_ready(env: Env) -> None:
    claim_id = await add_claim(env.sessions)

    with pytest.raises(NotReadyForAdjudication):
        await env.service(FakeModelGateway({})).draft(claim_id)


# --- The guardrail, through the graph's tools contract ------------------------------------


async def _snapshots(env: Env) -> tuple[UUID, dict[Stage, StageSnapshot]]:
    claim_id = await env.reviewed()
    records = await StageRecordRepository(env.sessions).latest_per_stage(claim_id)
    return claim_id, {
        stage: StageSnapshot(stage=stage, version=r.version, payload=r.payload)
        for stage, r in records.items()
    }


def _clauses() -> dict[str, PolicyClause]:
    return {
        ref: PolicyClause(product_uin=PRODUCT_UIN, clause_ref=ref, title=ref, text=ref)
        for ref in ("ROOM-CAP", "PROP-DED", "CO-PAY")
    }


async def test_guardrail_rejects_an_amount_that_differs_from_the_latest_sheet(env: Env) -> None:
    claim_id, snaps = await _snapshots(env)
    sheet = snaps[Stage.BILL_REVIEW]
    edited = StageSnapshot(
        stage=Stage.BILL_REVIEW,
        version=sheet.version + 1,
        payload={**sheet.payload, "payable": "120000.00"},
    )
    tools = FakeAdjudicationTools(
        {**{s: [v] for s, v in snaps.items()}, Stage.BILL_REVIEW: [sheet, edited]}, _clauses()
    )
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    draft = await run_adjudication(claim_id, gateway=gateway, tools=tools)

    assert draft.amount == PAYABLE  # copied from the sheet the draft was built on
    assert draft.guardrail_passed is False
    assert any("differs from the sheet payable Rs 120,000.00" in e for e in draft.guardrail_errors)
    assert tools.reads.count(Stage.BILL_REVIEW) == 2


async def test_guardrail_rejects_an_amount_over_the_available_balance(env: Env) -> None:
    claim_id, snaps = await _snapshots(env)
    reg = snaps[Stage.REGISTRATION]
    policy = {**reg.payload["policy"], "balance": "100000.00"}
    shrunk = StageSnapshot(
        stage=Stage.REGISTRATION, version=reg.version, payload={**reg.payload, "policy": policy}
    )
    tools = FakeAdjudicationTools(
        {**{s: [v] for s, v in snaps.items()}, Stage.REGISTRATION: [reg, shrunk]}, _clauses()
    )
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    draft = await run_adjudication(claim_id, gateway=gateway, tools=tools)

    assert draft.guardrail_passed is False
    assert any("exceeds the available balance" in e for e in draft.guardrail_errors)


# --- P2-1: human outcomes take effect -------------------------------------------------------


async def _append_verdict(
    sessions: async_sessionmaker[AsyncSession], claim_id: UUID, verdict: str
) -> None:
    repo = StageRecordRepository(sessions)
    latest = await repo.latest(claim_id, Stage.RISK)
    assert latest is not None
    report = {
        **latest.payload,
        "verdict": verdict,
        "verdict_note": "Forged bill",
        "verdict_by": "x",
    }
    await repo.append(claim_id, Stage.RISK, report, created_by="user:inv")


FRAUD_REJECT = {
    "decision": "reject",
    "reasons": [{"text": "The investigator confirmed fraud", "clause_ref": "FRAUD"}],
    "summary": "Fraud was confirmed.",
}


async def test_confirmed_fraud_makes_the_rule_reject_citing_the_fraud_clause(env: Env) -> None:
    claim_id = await env.reviewed()
    await _append_verdict(env.sessions, claim_id, "fraud_confirmed")
    gateway = FakeModelGateway({"adjudication_decide": [FRAUD_REJECT]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.expected_decision, draft.decision, draft.amount) == ("reject", "reject", 0)
    assert draft.guardrail_passed is True, draft.self_check_errors
    spec = gateway.calls[-1].schema
    assert spec is not None
    reason = spec.model_json_schema()["$defs"]["ReasonClosed"]["properties"]
    assert "FRAUD" in reason["clause_ref"]["anyOf"][0]["enum"]
    assert spec.model_json_schema()["properties"]["decision"]["enum"] == ["reject"]


async def test_a_cleared_verdict_leaves_the_decision_rules_as_they_were(env: Env) -> None:
    claim_id = await env.reviewed()
    await _append_verdict(env.sessions, claim_id, "cleared")
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    draft = await env.service(gateway).draft(claim_id)

    assert draft.expected_decision == "partial"


async def test_overruled_findings_are_ignored(env: Env) -> None:
    claim_id = await env.reviewed()
    overruled = Finding(
        finding_id=1,
        kind="eligibility",
        text="R5 failed: illness within the initial waiting period",
        severity="adverse",
        citations=[Citation(ref="clause:Excl03")],
        judged_by="person",
        rule_id="R5",
        overruled=True,
        overrule_reason="Portability: the waiting period is served",
    )
    standing = Finding(
        finding_id=2,
        kind="clinical",
        text="CT shows a localised collection",
        severity="info",
        citations=[Citation(ref="clause:ROOM-CAP")],
        judged_by="model",
    )
    repo = StageRecordRepository(env.sessions)
    latest = await repo.latest(claim_id, Stage.MEDICAL_REVIEW)
    assert latest is not None
    medical = MedicalFindings.model_validate(latest.payload).model_copy(
        update={
            "findings": [overruled, standing],
            "rule_results": [
                RuleResult(
                    rule_id="R5",
                    passed=True,
                    reason="overruled by a medical officer",
                    clause_ref="Excl03",
                    judged_by="person",
                )
            ],
        }
    )
    await repo.append(
        claim_id, Stage.MEDICAL_REVIEW, medical.model_dump(mode="json"), created_by="user:mo"
    )
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    draft = await env.service(gateway).draft(claim_id)

    assert draft.expected_decision == "partial"
    sent = " ".join(m.content for m in gateway.calls[-1].messages or [])
    assert standing.text in sent
    assert overruled.text not in sent
    spec = gateway.calls[-1].schema
    assert spec is not None
    reason = spec.model_json_schema()["$defs"]["ReasonClosed"]["properties"]
    assert "Excl03" not in reason["clause_ref"]["anyOf"][0]["enum"]


async def test_one_reason_naming_a_spread_rule_explains_every_line_it_spread_over(
    env: Env,
) -> None:
    """R20 spreads one package cap over many lines; one reason naming R20 explains them all.
    A line-specific deduction still needs its own reason."""
    # The worked bill as a cashless claim, so the agreement's package cap binds it.
    spec = ClaimSpec(request_type=RequestType.DISCHARGE)
    claim_id = await add_digitised_claim(env.sessions, spec)
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    await DefaultRegistrationService(
        env.sessions, env.policies, FakeMoneyService(), rules
    ).register(claim_id)
    await add_reference_rows(env.sessions)
    await add_coding(env.sessions, claim_id, procedures=LAP)
    bill = DefaultBillReviewService(
        env.sessions,
        rules,
        FakeModelGateway({"bill_package_choice": [{"procedure_1": "AG095"}]}),
        DefaultKnowledgeBase(env.sessions),
    )
    sheet = await bill.review(claim_id)
    await add_medical(env.sessions, claim_id)
    assert {ln.rule_id for ln in sheet.lines if ln.deducted > 0} == {"R20"}
    package = {"text": "Paid at the agreed AG095 package rate", "clause_ref": "AGREED-TARIFF",
               "rule_id": "R20", "line_id": None}  # fmt: skip
    reply = {"decision": "partial", "reasons": [package], "summary": "Package rate."}
    gateway = FakeModelGateway({"adjudication_decide": [reply]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.self_check_errors, draft.guardrail_passed) == ([], True)
    assert draft.amount == sheet.payable


async def test_one_reason_naming_r26_explains_every_line_it_spread_over(env: Env) -> None:
    """R26 (the final bill against the approved pre-auth) is a claim-level rule spread over
    the lines like R20, so one reason naming it explains them all (P2-5)."""
    spec = ClaimSpec(request_type=RequestType.DISCHARGE)
    claim_id = await add_digitised_claim(env.sessions, spec)
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    await DefaultRegistrationService(
        env.sessions, env.policies, FakeMoneyService(), rules
    ).register(claim_id)
    await add_reference_rows(env.sessions)
    await add_coding(env.sessions, claim_id, procedures=LAP)
    bill = DefaultBillReviewService(
        env.sessions,
        rules,
        FakeModelGateway({"bill_package_choice": [{"procedure_1": "AG095"}]}),
        DefaultKnowledgeBase(env.sessions),
    )
    sheet = await bill.review(claim_id)
    # The same spread, as R26 took it: every deducted line names R26.
    held = sheet.model_dump(mode="json")
    for line in held["lines"]:
        if line["rule_id"] == "R20":
            line["rule_id"], line["clause_ref"] = "R26", "PREAUTH"
    for result in held["rule_results"]:
        if result["rule_id"] == "R20":
            result["rule_id"], result["clause_ref"] = "R26", "PREAUTH"
    records = StageRecordRepository(env.sessions)
    await records.append(claim_id, Stage.BILL_REVIEW, held, created_by="test")
    await add_medical(env.sessions, claim_id)
    held_r26 = {"text": "Held to the approved pre-auth", "clause_ref": "PREAUTH",
                "rule_id": "R26", "line_id": None}  # fmt: skip
    reply = {"decision": "partial", "reasons": [held_r26], "summary": "Pre-auth limit."}
    gateway = FakeModelGateway({"adjudication_decide": [reply]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.self_check_errors, draft.guardrail_passed) == ([], True)


async def test_a_document_in_any_classified_set_is_not_outstanding(env: Env) -> None:
    """A claim's documents arrive in several sets: the bundle, a query reply, and a
    reconsideration's new evidence. A document any set holds is received, not only one in
    the latest set."""
    claim_id = await env.reviewed()
    repo = StageRecordRepository(env.sessions)
    intake = await repo.latest(claim_id, Stage.INTAKE)
    assert intake is not None
    await repo.append(claim_id, Stage.INTAKE,
                      {**intake.payload, "missing_documents": ["payment_receipt"]},
                      created_by="test")  # fmt: skip
    for doc_type in ("payment_receipt", "other"):  # the reply, then the reconsideration
        await repo.append(claim_id, Stage.CLASSIFICATION,
                          {"submission_id": str(uuid4()), "documents": [{"doc_type": doc_type}]},
                          created_by="test")  # fmt: skip
    gateway = FakeModelGateway({"adjudication_decide": [PARTIAL]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.expected_decision, draft.decision) == ("partial", "partial")


# --- Phase 3: a query the claimant never answered -----------------------------------------


async def test_an_unanswered_query_drafts_a_reject_for_non_submission(env: Env) -> None:
    """The query's deadline passed with no reply: code drafts a reject for non-submission of
    the documents asked for, citing the claim documents clause, with no model call. The CRC
    decides it. A later draft on the same inputs returns this reject, not a new query."""
    claim_id = await env.reviewed(query_items=[CT_REPORT])
    gateway = FakeModelGateway({"adjudication_decide": [query_reply("investigation_report")]})
    service = env.service(gateway)
    await service.draft(claim_id)

    rejected = await service.draft_unanswered(claim_id, ["CT abdomen report"])

    assert (rejected.decision, rejected.expected_decision) == ("reject", "reject")
    assert rejected.amount == D("0.00") and rejected.query_items == []
    [reason] = rejected.reasons
    assert reason.clause_ref == DOCUMENTS_CLAUSE
    assert "CT abdomen report" in reason.text
    assert rejected.guardrail_passed is True
    assert len(gateway.calls) == 1  # the query draft only
    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.ADJUDICATION)
    assert stored is not None and stored.created_by == "code"
    assert await service.draft(claim_id) == rejected
    clause = await DefaultKnowledgeBase(env.sessions).get_clause(PRODUCT_UIN, DOCUMENTS_CLAUSE)
    assert clause is not None
