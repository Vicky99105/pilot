"""Builders for claims that have reached adjudication, through the real registration, bill
review and adjudication services. Used by the decision, communication and settlement tests."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.integrations.core.contract import MemberPolicy
from app.services.adjudication.service import DefaultAdjudicationService
from app.services.bill_review.service import DefaultBillReviewService
from app.services.decision.contract import ApproverOutcome, CrcOutcome
from app.services.decision.service import DefaultDecisionService
from app.services.digitisation.contract import Bank, BillLine
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import DOCTOR_USER_ID, MEMBER_USER_ID, SENIOR_USER_ID
from app.shared.types import Field, Stage
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import ClaimSpec, add_digitised_claim, bill_line
from tests.services.test_adjudication import PARTIAL as WORKED_PARTIAL
from tests.services.test_adjudication import add_medical
from tests.services.test_registration import asha_policy

D = Decimal
Kind = Literal["partial", "approve", "reject", "worked"]

# Room Rs 6,000 a day for 3 days against a Rs 5,000 cap: lines 1 and 2 are reduced.
# Admissible Rs 38,250, co-pay Rs 1,912.50, payable Rs 36,337.50: within the approver band.
BREACH_BILL = [
    bill_line(1, "Room Charges", "Single room", "18000", qty="3"),
    bill_line(2, "Professional Fees", "Surgeon visits", "6000"),
    bill_line(3, "Pharmacy", "Drugs and medicines", "8250"),
    bill_line(4, "Investigations", "CT scan and blood tests", "10000"),
]
BREACH_PAYABLE = D("36337.50")
CLEAN_BILL = [
    bill_line(1, "Room Charges", "Twin sharing room", "12000", qty="3"),
    bill_line(2, "Pharmacy", "Drugs and medicines", "8250"),
]
CLEAN_PAYABLE = D("19237.50")  # 20,250 less 5% co-pay

PARTIAL = {
    "decision": "partial",
    "reasons": [
        {"text": "Room above the cap", "clause_ref": "ROOM-CAP", "rule_id": "R13", "line_id": 1},
        {"text": "Fees in proportion", "clause_ref": "PROP-DED", "rule_id": "R13", "line_id": 2},
    ],
    "summary": "Room cap breached.",
}
APPROVE = {"decision": "approve", "reasons": [{"text": "All checks passed", "rule_id": "R21"}],
           "summary": "Payable in full after co-pay."}  # fmt: skip
REJECT = {
    "decision": "reject",
    "reasons": [{"text": "Within the 30-day wait", "clause_ref": "Excl03", "rule_id": "R5"}],
    "summary": "Admitted within the initial waiting period.",
}


def bank(
    holder: str = "ASHA VERMA", account: str = "123456789012", ifsc: str = "SBIN0001234"
) -> Bank:
    def f(value: str) -> Any:
        return Field(value=value, confidence=0.99)

    return Bank(account_number=f(account), ifsc=f(ifsc), account_holder=f(holder))


@dataclass
class Adjudicated:
    claim_id: UUID
    money: FakeMoneyService
    policies: FakePolicyService


@dataclass
class Plan:
    lines: list[BillLine]
    reply: dict[str, Any]
    policy: MemberPolicy = field(default_factory=asha_policy)


PLANS: dict[Kind, Plan] = {
    "partial": Plan(BREACH_BILL, PARTIAL),
    "approve": Plan(CLEAN_BILL, APPROVE),
    # Cover began 10 days before admission: R5, the 30-day initial wait, fails.
    "reject": Plan(
        CLEAN_BILL,
        REJECT,
        asha_policy(inception_date=date(2026, 10, 23), continuous_cover_start=date(2026, 10, 23)),
    ),
    "worked": Plan([], WORKED_PARTIAL),  # ClaimSpec's default: the worked example, Rs 1,33,000
}


async def adjudicated_claim(
    sessions: async_sessionmaker[AsyncSession],
    kind: Kind = "partial",
    *,
    cheque: Bank | None = None,
) -> Adjudicated:
    """A claim through registration, bill review, a clean medical review and risk, and
    adjudication. The cheque and Part A carry the same bank account unless cheque is given."""
    plan = PLANS[kind]
    spec = ClaimSpec(lines=plan.lines) if plan.lines else ClaimSpec()
    if plan.lines:
        spec.claimed = sum((ln.amount.value or D(0) for ln in plan.lines), D(0))
    claim_id = await add_digitised_claim(sessions, spec)
    async with sessions.begin() as session:  # the member submitted it; letters go to them
        claim = await session.get(ClaimRow, claim_id)
        assert claim is not None
        await session.execute(
            update(SubmissionRow)
            .where(SubmissionRow.id == claim.submission_id)
            .values(submitted_by=MEMBER_USER_ID)
        )
    records = StageRecordRepository(sessions)
    latest = await records.latest(claim_id, Stage.DIGITISATION)
    assert latest is not None
    payload = dict(latest.payload)
    payload["bank"] = (cheque or bank()).model_dump(mode="json")
    payload["claim_form_bank"] = bank().model_dump(mode="json")
    await records.append(claim_id, Stage.DIGITISATION, payload, created_by="test")

    policies, money = FakePolicyService([plan.policy]), FakeMoneyService()
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    await DefaultRegistrationService(sessions, policies, money, rules).register(claim_id)
    await DefaultBillReviewService(sessions, rules).review(claim_id)
    await add_medical(sessions, claim_id)
    gateway = FakeModelGateway({"adjudication_decide": [plan.reply]})
    draft = await DefaultAdjudicationService(
        sessions, gateway, DefaultKnowledgeBase(sessions)
    ).draft(claim_id)
    assert draft.guardrail_passed, (draft.self_check_errors, draft.guardrail_errors)
    expected = "partial" if kind == "worked" else kind
    assert draft.decision == expected
    return Adjudicated(claim_id, money, policies)


async def decided_claim(
    sessions: async_sessionmaker[AsyncSession],
    kind: Kind = "partial",
    *,
    cheque: Bank | None = None,
) -> Adjudicated:
    """An adjudicated claim the approver approved as drafted and, for a partial or reject,
    the CRC upheld."""
    claim = await adjudicated_claim(sessions, kind, cheque=cheque)
    decisions = DefaultDecisionService(sessions, Settings().config_dir / "authority_matrix.yaml")
    approver = SENIOR_USER_ID if kind == "worked" else DOCTOR_USER_ID
    record = await decisions.record(
        claim.claim_id, ApproverOutcome(action="approve", approver_id=approver)
    )
    if record.crc_required:
        await decisions.review_crc(
            claim.claim_id, CrcOutcome(action="uphold", reviewer_id=DOCTOR_USER_ID)
        )
    return claim
