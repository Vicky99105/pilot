"""Adjudication through its contract (and the graph through its tools contract): the model
drafts, code self-checks, copies the amount and runs the guardrail."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.adjudication_graph import run_adjudication
from app.agents.tools import StageSnapshot
from app.config import Settings
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.adjudication.contract import (
    AdjudicationService,
    DraftDecision,
    NotReadyForAdjudication,
)
from app.services.adjudication.service import DefaultAdjudicationService
from app.services.bill_review.service import DefaultBillReviewService
from app.services.knowledge_base.contract import PolicyClause
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import MedicalFindings
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.contract import RiskReport
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import PRODUCT_UIN
from app.shared.types import Stage
from tests.database.factories import add_claim
from tests.fakes.adjudication import FakeAdjudicationTools
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import ClaimSpec, add_digitised_claim
from tests.services.test_registration import asha_policy

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
    sessions: async_sessionmaker[AsyncSession], claim_id: UUID, query_items: list[str] | None = None
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

    async def reviewed(self, query_items: list[str] | None = None) -> UUID:
        """A claim through registration, bill review, medical review and risk."""
        claim_id = await add_digitised_claim(self.sessions, ClaimSpec())
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        await DefaultRegistrationService(
            self.sessions, self.policies, FakeMoneyService(), rules
        ).register(claim_id)
        await DefaultBillReviewService(self.sessions, rules).review(claim_id)
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


async def test_medical_query_items_give_a_query(env: Env) -> None:
    claim_id = await env.reviewed(query_items=["CT abdomen report"])
    query = {
        "decision": "query",
        "reasons": [{"text": "Imaging report needed", "rule_id": "R13", "line_id": 1}],
        "summary": "Asking for the CT report.",
        "query_items": ["CT abdomen report"],
    }
    gateway = FakeModelGateway({"adjudication_decide": [query]})

    draft = await env.service(gateway).draft(claim_id)

    assert (draft.decision, draft.amount, draft.query_items) == (
        "query",
        D("0.00"),
        ["CT abdomen report"],
    )
    assert draft.guardrail_passed is True


async def test_a_decision_that_contradicts_the_rules_fails_the_guardrail(env: Env) -> None:
    claim_id = await env.reviewed()
    approve = {"decision": "approve", "reasons": ALL_LINES, "summary": "Looks fine."}
    gateway = FakeModelGateway({"adjudication_decide": [approve, approve]})

    draft = await env.service(gateway).draft(claim_id)

    assert draft.revisions == 1
    assert any("expected partial" in e for e in draft.self_check_errors)
    assert draft.guardrail_passed is False


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
