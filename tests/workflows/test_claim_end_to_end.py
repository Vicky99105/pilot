"""The tracer acceptance claim, intake to CLOSED, through every real stage service.

Time-skipping Temporal, SQLite, an in-memory file store, a scripted model gateway, fake
core policy and money services, and a fake notification sender. The claim is a member's
reimbursement with a room-rent breach and a missing payment receipt: the breach is deducted
under R13, a query is raised and answered by a reply upload, the partial is approved and
upheld by the CRC, a letter is issued, finance releases the payment, and the claim closes.
"""

import asyncio
import copy
import re
from collections.abc import AsyncIterator, Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.config import Settings
from app.database.models import ClaimRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.adjudication.service import DefaultAdjudicationService
from app.services.bill_review.service import DefaultBillReviewService
from app.services.classification.service import DefaultClassificationService
from app.services.communication.service import PLAIN_PURPOSE, DefaultCommunicationService
from app.services.decision.service import DefaultDecisionService
from app.services.digitisation.service import DefaultDigitisationService
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService, DefaultWorkflowStarter
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.service import DefaultMedicalReviewService
from app.services.model_gateway.contract import Message, ModelRole, T
from app.services.monitoring.service import DefaultMonitoring
from app.services.page_prep.service import DefaultPagePreparer
from app.services.query.service import DefaultQueryService
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.service import DefaultRiskService
from app.services.rules.service import DefaultRulesEngine
from app.services.settlement.service import DefaultSettlementService
from app.shared.seed_ids import (
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    OPS_USER_ID,
)
from app.shared.types import DocType, RequestType, Stage
from app.workflows.claim_activities import ClaimActivities
from app.workflows.claim_workflow import ClaimWorkflow
from app.workflows.worker import build_worker
from tests.docs import make_pdf
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.notifications import FakeNotificationService
from tests.fakes.tasks import FakeTaskService
from tests.services.claim_builders import DIAGNOSIS
from tests.services.test_digitisation import CHEQUE, PART_A, line, section, v
from tests.services.test_medical_review import APPENDIX_TERM
from tests.services.test_registration import asha_policy

TASK_QUEUE = "test-claims-end-to-end"
D = Decimal

# Room Rs 6,000 a day for 3 days against the Rs 5,000 cap (2% of Rs 3 lakh, capped).
BILL: dict[str, Any] = {
    "bill_number": v("SMH/IP/2026/1190"),
    "patient_name": v("Asha Verma"),
    "lines": [
        line("Room Charges", "Single room", "3", "6000", "18000"),
        line("Professional Fees", "Surgeon visits", "4", "1500", "6000"),
        line("Pharmacy", "Drugs and IV fluids", "1", "8250", "8250", page=2),
        line("Investigations", "CT abdomen and blood tests", "1", "10000", "10000", page=2),
    ],
    "section_totals": [
        section("Room Charges", "18000"),
        section("Professional Fees", "6000"),
        section("Pharmacy", "8250"),
        section("Investigations", "10000"),
    ],
    "grand_total": v("42250", page=2),
}
CLAIMED = "42250"
PART_A_REPLY = {
    **copy.deepcopy(PART_A),
    "claimed_hospital_expenses": v(CLAIMED),
    "claimed_total": v("42,250.00"),
}
DISCHARGE: dict[str, Any] = {
    "patient_name": v("Mrs. ASHA VERMA"),
    "admission_date": v("2026-11-02"),
    "discharge_date": v("2026-11-05"),
    "diagnoses": [v(DIAGNOSIS)],
    "procedures": [v("Laparoscopic appendicectomy")],
    "icd_codes_written": [v("K35.8")],
    "facts": [
        {
            "kind": "finding",
            "text": "CT abdomen: perforated appendix with localised collection",
            "page": 1,
            "confidence": 0.95,
        }
    ],
}
# Uploaded as typed slots; the payment receipt is missing. The bill has two pages.
UPLOADS = [
    (DocType.CLAIM_FORM_PART_A, ["PART A"]),
    (DocType.CLAIM_FORM_PART_B, ["PART B"]),
    (DocType.DISCHARGE_SUMMARY, ["DISCHARGE SUMMARY"]),
    (DocType.FINAL_BILL, ["FINAL BILL page 1", "FINAL BILL page 2"]),
    (DocType.CANCELLED_CHEQUE, ["CHEQUE"]),
]
REWRITE = {"items": [{"phrase": DIAGNOSIS, "index_terms": [APPENDIX_TERM]}]}
CHOOSE = {
    "codes": [
        {
            "code": "K35.32",
            "phrase": DIAGNOSIS,
            "citations": [{"ref": "icd:K35.32"}],
            "rationale": "Perforation with localised peritonitis on CT",
        }
    ],
    "findings": [],
    "query_items": [],
}
QUERY_DRAFT = {
    "decision": "query",
    "reasons": [],
    "summary": "The payment receipt is missing.",
    "query_items": ["Payment receipt"],
}
PARTIAL_DRAFT = {
    "decision": "partial",
    "reasons": [
        {"text": "Room above the cap", "clause_ref": "ROOM-CAP", "rule_id": "R13", "line_id": 1},
        {"text": "Fees in proportion", "clause_ref": "PROP-DED", "rule_id": "R13", "line_id": 2},
    ],
    "summary": "Room cap breached; proportionate deduction applied.",
}
PAYABLE = D("36337.50")  # admissible Rs 38,250 less the 5% co-pay


class Gateway(FakeModelGateway):
    """The script, plus a faithful plain-language rewrite built from the call's reasons."""

    async def structured(
        self,
        role: ModelRole,
        messages: list[Message],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        if purpose == PLAIN_PURPOSE:
            raw = re.findall(r"^\d+\. (.*)$", messages[-1].content, flags=re.M)
            self._script[purpose] = [{"items": [f"Put simply: {r}" for r in raw]}]
        return await super().structured(role, messages, schema, claim_id=claim_id, purpose=purpose)


def _gateway() -> Gateway:
    pages = [doc for doc, texts in UPLOADS for _ in texts]
    labels = [{"doc_type": d.value, "confidence": 0.96} for d in pages]
    return Gateway(
        {
            # The claim's 6 pages, then the reply's 1 page.
            "classify_page": [*labels, {"doc_type": "payment_receipt", "confidence": 0.97}],
            "extract_claim_form_part_a": [PART_A_REPLY],
            "extract_discharge_summary": [DISCHARGE],
            "extract_final_bill": [BILL],
            "extract_cancelled_cheque": [CHEQUE],
            # Medical review runs again after the reply.
            "medical_rewrite_terms": [REWRITE, REWRITE],
            "medical_choose_codes": [CHOOSE, CHOOSE],
            "adjudication_decide": [QUERY_DRAFT, PARTIAL_DRAFT],
        }
    )


class Harness:
    def __init__(
        self, env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        self.env = env
        self.sessions = sessions
        self.files = FakeFileStore()
        self.gateway = _gateway()
        self.money = FakeMoneyService()
        self.notifications = FakeNotificationService()
        self.tasks = FakeTaskService()
        self.monitoring = DefaultMonitoring(sessions)
        self.records = StageRecordRepository(sessions)
        starter = DefaultWorkflowStarter(
            Settings(temporal_task_queue=TASK_QUEUE), client=env.client
        )
        self.intake = DefaultIntakeService(sessions, self.files, starter, signaller=starter)
        config = Settings().config_dir
        rules = DefaultRulesEngine(config / "products")
        kb = DefaultKnowledgeBase(sessions)
        policies = FakePolicyService([asha_policy()])
        communication = DefaultCommunicationService(
            sessions, self.files, self.gateway, self.notifications
        )
        self.queries = DefaultQueryService(
            sessions, communication, reply_days=15, reminder_days=[3, 7]
        )
        self.activities = ClaimActivities(
            self.monitoring,
            self.tasks,
            intake=self.intake,
            classification=DefaultClassificationService(
                sessions, self.files, self.gateway, DefaultPagePreparer(sessions, self.files)
            ),
            digitisation=DefaultDigitisationService(sessions, self.files, self.gateway),
            registration=DefaultRegistrationService(sessions, policies, self.money, rules),
            bill_review=DefaultBillReviewService(sessions, rules),
            medical_review=DefaultMedicalReviewService(sessions, self.gateway, kb, rules, policies),
            risk=DefaultRiskService(sessions, self.gateway, policies),
            adjudication=DefaultAdjudicationService(sessions, self.gateway, kb),
            decision=DefaultDecisionService(sessions, config / "authority_matrix.yaml"),
            communication=communication,
            query=self.queries,
            settlement=DefaultSettlementService(sessions, self.money),
        )

    async def submit(self) -> Submission:
        return await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=MEMBER_USER_ID,
                channel="member",
                form=ClaimForm(
                    policy_number=MEMBER_POLICY_NUMBER,
                    member_id=MEMBER_MEMBER_ID,
                    patient_name="Asha Verma",
                    admission_date=date(2026, 11, 2),
                    discharge_date=date(2026, 11, 5),
                    claimed_amount=D(CLAIMED),
                ),
                files=[
                    UploadedFile(filename=f"{doc.value}.pdf", data=make_pdf(texts), slot=doc)
                    for doc, texts in UPLOADS
                ],
            )
        )

    async def wait_for(self, check: Callable[[], Any], what: str) -> Any:
        for _ in range(600):
            found = check()
            if asyncio.iscoroutine(found):
                found = await found
            if found:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    async def complete(self, kind: str, user: UUID, outcome: dict[str, Any]) -> None:
        """What the task route does: TaskService.complete, which signals the workflow."""
        await self.wait_for(lambda: self.tasks.unfinished(kind), f"task {kind}")
        [task] = self.tasks.unfinished(kind)
        self.tasks.roles.setdefault(user, set()).add(task.role)
        await self.tasks.complete(task_id=task.id, user_id=user, outcome=outcome)
        _, task_id, signalled = self.tasks.signaller.signals[-1]
        handle = self.env.client.get_workflow_handle(task.workflow_id)
        await handle.signal(ClaimWorkflow.task_completed, args=[str(task_id), signalled])


@pytest.fixture
async def harness(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    await seed_claims_db(sessions)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        h = Harness(env, sessions)
        async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
            yield h


async def test_the_tracer_claim_runs_from_intake_to_closed(harness: Harness) -> None:
    h = harness
    submission = await h.submit()
    claim_id = submission.claim_id
    assert submission.missing_documents == [DocType.PAYMENT_RECEIPT]

    await h.complete("confirm_registration", OPS_USER_ID, {"action": "confirm"})
    await h.complete("confirm_deductions", OPS_USER_ID, {"action": "confirm"})

    query = await h.wait_for(lambda: h.queries.open_query(claim_id), "the query")
    assert [i.text for i in query.items] == ["Payment receipt"]
    reply = await h.intake.accept_followup(
        claim_id,
        submitted_by=MEMBER_USER_ID,
        channel="member",
        files=[
            UploadedFile(
                filename="receipt.pdf", data=make_pdf(["RECEIPT"]), slot=DocType.PAYMENT_RECEIPT
            )
        ],
    )

    await h.complete("approve", DOCTOR_USER_ID, {"action": "approve"})
    await h.complete("crc", DOCTOR_USER_ID, {"action": "uphold"})
    await h.complete("release_payment", FINANCE_USER_ID, {"action": "release"})
    result = await h.env.client.get_workflow_handle_for(ClaimWorkflow.run, str(claim_id)).result()

    # The breach is deducted under R13 with its clause.
    sheet = await h.records.latest(claim_id, Stage.BILL_REVIEW)
    assert sheet is not None
    room = sheet.payload["lines"][0]
    assert (room["rule_id"], room["clause_ref"], room["deducted"]) == ("R13", "ROOM-CAP", "3000.00")
    assert sheet.payload["payable"] == str(PAYABLE)

    # The query was raised once and answered by the reply.
    answered = await h.queries.get(query.query_id)
    assert answered is not None
    assert (answered.status, answered.reply_submission_id) == ("answered", reply.submission_id)
    assert await h.queries.open_query(claim_id) is None

    # Letters: the query letter, then the partial with its deduction table.
    letters = [r.payload for r in await h.records.history(claim_id, Stage.COMMUNICATION)]
    assert [ltr["kind"] for ltr in letters] == ["query", "partial"]
    partial = letters[-1]
    assert (partial["amount"], partial["plain_language"]) == (str(PAYABLE), True)
    assert [row["line_id"] for row in partial["deductions"]] == [1, 2, None]
    assert await h.files.get(partial["pdf_key"])
    assert [n.to for n in h.notifications.sent] == ["asha.verma@member.example"] * 2

    # One payment instruction, after the release; the block released with the paid amount.
    [(key, payment)] = h.money.payments.items()
    assert key.startswith(f"{claim_id}pay") and payment.amount == PAYABLE
    [(_, utilised)] = h.money.releases.values()
    assert utilised == PAYABLE

    assert (result.status, result.draft) == ("closed", "partial")
    async with h.sessions() as session:
        row = await session.get(ClaimRow, claim_id)
    assert row is not None and (row.status, row.closed_at is not None) == ("closed", True)

    events = [(e.stage.value, e.action) for e in await h.monitoring.events(claim_id)]
    assert events == EXPECTED_EVENTS
    opened = [
        e.detail["kind"]  # type: ignore[index]
        for e in await h.monitoring.events(claim_id)
        if e.action == "checkpoint_opened"
    ]
    assert opened == ["confirm_registration", "confirm_deductions", "approve", "crc",
                      "release_payment"]  # fmt: skip


EXPECTED_EVENTS = [
    ("intake", "stage_completed"),
    ("classification", "stage_completed"),
    ("digitisation", "stage_completed"),
    ("registration", "stage_completed"),
    ("registration", "checkpoint_opened"),
    ("registration", "checkpoint_completed"),
    ("bill_review", "stage_completed"),
    ("bill_review", "checkpoint_opened"),
    ("bill_review", "checkpoint_completed"),
    ("medical_review", "stage_completed"),
    ("risk", "stage_completed"),
    ("adjudication", "stage_completed"),  # draft: query
    ("query", "stage_completed"),  # query raised, query letter issued
    ("query", "query_answered"),
    ("classification", "stage_completed"),  # the reply's pages
    ("digitisation", "stage_completed"),  # merged into a new record version
    ("medical_review", "stage_completed"),
    ("risk", "stage_completed"),
    ("adjudication", "stage_completed"),  # draft: partial
    ("decision", "decision_routed"),
    ("decision", "checkpoint_opened"),  # approve
    ("decision", "checkpoint_completed"),
    ("decision", "decision_recorded"),
    ("decision", "checkpoint_opened"),  # crc
    ("decision", "checkpoint_completed"),
    ("decision", "crc_reviewed"),
    ("decision", "stage_completed"),
    ("communication", "stage_completed"),  # the partial letter
    ("settlement", "payee_checked"),
    ("settlement", "checkpoint_opened"),  # release payment
    ("settlement", "checkpoint_completed"),
    ("settlement", "stage_completed"),  # payment instruction and block release
    ("settlement", "claim_closed"),
    ("monitoring", "stage_completed"),
]
