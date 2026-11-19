"""ClaimWorkflow with the real intake, classification, digitisation, registration and
bill review services.

Time-skipping Temporal, SQLite, an in-memory file store, a scripted model gateway and fake
core policy and money services.
"""

import asyncio
import copy
from collections.abc import AsyncIterator, Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.config import Settings
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.integrations.openai_compatible import ProviderPermanentError
from app.services.bill_review.service import DefaultBillReviewService
from app.services.classification.service import DefaultClassificationService
from app.services.digitisation.service import DefaultDigitisationService
from app.services.intake.contract import (
    ClaimForm,
    Submission,
    SubmissionInput,
    UploadedFile,
    WorkflowAlreadyStarted,
)
from app.services.intake.service import DefaultIntakeService, DefaultWorkflowStarter
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import MedicalCoding
from app.services.page_prep.service import DefaultPagePreparer
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import RequestType, Stage
from app.workflows.claim_activities import ClaimActivities
from app.workflows.claim_workflow import ClaimWorkflow
from app.workflows.worker import build_worker
from tests.docs import make_pdf
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.stages import (
    FakeAdjudicationService,
    FakeCommunicationService,
    FakeDecisionService,
    FakeMedicalReviewService,
    FakeQueryService,
    FakeRiskService,
    FakeSettlementService,
)
from tests.fakes.tasks import FakeTaskService
from tests.services.claim_builders import add_coding
from tests.services.test_digitisation import BILL, CHEQUE, DISCHARGE, PAGES, PART_A, v
from tests.services.test_registration import asha_policy

TASK_QUEUE = "test-claims-slice-2"


class CodingMedicalReview(FakeMedicalReviewService):
    """Stores a coding record with no procedure, so the real bill review can run."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self.sessions = sessions

    async def code(self, claim_id: UUID) -> MedicalCoding:
        return await add_coding(self.sessions, claim_id, procedures=())


class Harness:
    def __init__(
        self,
        env: WorkflowEnvironment,
        sessions: async_sessionmaker[AsyncSession],
        gateway: FakeModelGateway,
    ) -> None:
        self.env = env
        self.sessions = sessions
        self.files = FakeFileStore()
        self.tasks = FakeTaskService()
        self.monitoring = FakeMonitoring()
        self.gateway = gateway
        self.starter = DefaultWorkflowStarter(
            Settings(temporal_task_queue=TASK_QUEUE), client=env.client
        )
        self.intake = DefaultIntakeService(sessions, self.files, self.starter)
        self.money = FakeMoneyService()
        self.activities = self.build_activities(gateway)

    def build_activities(self, gateway: Any) -> ClaimActivities:
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        return ClaimActivities(
            self.monitoring,
            self.tasks,
            intake=self.intake,
            classification=DefaultClassificationService(
                self.sessions, self.files, gateway, DefaultPagePreparer(self.sessions, self.files)
            ),
            digitisation=DefaultDigitisationService(self.sessions, self.files, gateway),
            registration=DefaultRegistrationService(
                self.sessions, FakePolicyService([asha_policy()]), self.money, rules
            ),
            bill_review=DefaultBillReviewService(
                self.sessions, rules, gateway, DefaultKnowledgeBase(self.sessions)
            ),
            medical_review=CodingMedicalReview(self.sessions),
            risk=FakeRiskService(),
            adjudication=FakeAdjudicationService(),
            decision=FakeDecisionService(),
            communication=FakeCommunicationService(),
            query=FakeQueryService(),
            settlement=FakeSettlementService(),
        )

    async def submit(self) -> Submission:
        return await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=MEMBER_USER_ID,
                channel="member",
                form=ClaimForm(
                    policy_number="MOCK/AS/2026/000101",
                    member_id="MOCK-M-000101-01",
                    patient_name="Asha Verma",
                    admission_date=date(2026, 11, 2),
                    discharge_date=date(2026, 11, 5),
                    claimed_amount=Decimal("48250"),
                    declaration_accepted=True,
                ),
                files=[UploadedFile(filename="all.pdf", data=make_pdf([t for _, t in PAGES]))],
            )
        )

    async def wait_for(self, check: Callable[[], bool], what: str) -> None:
        for _ in range(400):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    async def complete(self, kind: str, outcome: dict[str, Any]) -> str:
        await self.wait_for(lambda: bool(self.tasks.unfinished(kind)), f"task {kind}")
        [task] = self.tasks.unfinished(kind)
        self.tasks.roles.setdefault(MEMBER_USER_ID, set()).add(task.role)
        await self.tasks.complete(task_id=task.id, user_id=MEMBER_USER_ID, outcome=outcome)
        handle = self.env.client.get_workflow_handle(task.workflow_id)
        signalled = {**outcome, "completed_by": str(MEMBER_USER_ID)}  # as TaskService sends it
        await handle.signal(ClaimWorkflow.task_completed, args=[str(task.id), signalled])
        return str(task.id)


def _script(
    part_a: dict[str, Any] | None = None, unsure_page: int | None = None
) -> FakeModelGateway:
    labels = [{"doc_type": t.value, "confidence": 0.95} for t, _ in PAGES]
    if unsure_page is not None:
        labels[unsure_page] = {"doc_type": "other", "confidence": 0.5}
    return FakeModelGateway(
        {
            "classify_page": labels,
            "extract_claim_form_part_a": [part_a or PART_A],
            "extract_discharge_summary": [DISCHARGE],
            "extract_final_bill": [BILL],
            "extract_cancelled_cheque": [CHEQUE],
        }
    )


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        yield env


async def _harness(
    env: WorkflowEnvironment,
    sessions: async_sessionmaker[AsyncSession],
    gateway: FakeModelGateway,
) -> Harness:
    await seed_claims_db(sessions)
    return Harness(env, sessions, gateway)


async def test_a_low_confidence_field_opens_fix_fields_and_resumes_after_it(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    part_a = copy.deepcopy(PART_A)
    part_a["patient_name"] = v("Asha Varma", confidence=0.6)
    h = await _harness(env, sessions, _script(part_a))
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        assert submission.workflow_id == str(submission.claim_id)

        await h.wait_for(lambda: bool(h.tasks.unfinished("fix_fields")), "fix fields task")
        [task] = h.tasks.unfinished("fix_fields")
        assert task.payload["low_confidence_fields"] == ["identity.patient_name"]
        # The current value travels with the task, so the screen can show and cite it.
        flagged = task.payload["fields"]["identity.patient_name"]
        assert (flagged["value"], flagged["confidence"]) == ("Asha Varma", 0.6)
        assert h.tasks.unfinished("confirm_registration") == []

        task_id = await h.complete(
            "fix_fields", {"action": "confirm", "fields": {"identity.patient_name": "Asha Verma"}}
        )
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_registration")), "registration checkpoint"
        )

        history = await StageRecordRepository(sessions).history(
            submission.claim_id, Stage.DIGITISATION
        )
        assert [r.created_by for r in history] == ["model", f"task:{task_id}"]
        assert history[-1].payload["low_confidence_fields"] == []
        assert history[-1].payload["identity"]["patient_name"]["value"] == "Asha Verma"
        assert "fix_labels" not in [
            e.detail["kind"]  # type: ignore[index]
            for e in h.monitoring.recorded
            if e.action == "checkpoint_opened"
        ]
        assert "corrections_applied" in h.monitoring.actions(Stage.DIGITISATION)
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


async def test_unsure_labels_open_fix_labels_and_the_new_labels_are_stored(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, _script(unsure_page=1))
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        await h.wait_for(lambda: bool(h.tasks.unfinished("fix_labels")), "fix labels task")
        [task] = h.tasks.unfinished("fix_labels")
        unsure = next(p for p in task.payload["pages"] if p["confidence"] < 0.8)

        await h.complete(
            "fix_labels", {"action": "confirm", "labels": {unsure["page_id"]: "discharge_summary"}}
        )
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_registration")), "registration checkpoint"
        )

        latest = await StageRecordRepository(sessions).latest(
            submission.claim_id, Stage.CLASSIFICATION
        )
        assert latest is not None and latest.version == 2
        assert latest.payload["needs_review"] is False
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


async def test_refused_corrections_reopen_the_checkpoint(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    part_a = copy.deepcopy(PART_A)
    part_a["patient_name"] = v("Asha Verma", confidence=0.6)
    h = await _harness(env, sessions, _script(part_a))
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        await h.complete("fix_fields", {"fields": {"identity.no_such_field": "x"}})
        await h.wait_for(
            lambda: "corrections_rejected" in h.monitoring.actions(Stage.DIGITISATION),
            "rejection event",
        )
        await h.wait_for(lambda: bool(h.tasks.unfinished("fix_fields")), "fix fields again")
        [again] = h.tasks.unfinished("fix_fields")

        assert "no field at identity.no_such_field" in again.payload["correction_error"]
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


async def test_a_permanent_model_failure_fails_the_stage_with_an_event(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, FakeModelGateway({}))  # no scripted replies
    h.activities = h.build_activities(_FailingGateway())
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        handle = env.client.get_workflow_handle(str(submission.claim_id))

        with pytest.raises(Exception, match="Workflow execution failed"):
            await handle.result()

        assert (await handle.describe()).status == WorkflowExecutionStatus.FAILED
        failed = [e for e in h.monitoring.recorded if e.action == "stage_failed"]
        assert [e.stage for e in failed] == [Stage.CLASSIFICATION]
        assert "no API key" in failed[0].detail["error"]  # type: ignore[index]


async def test_a_second_start_with_the_same_claim_id_is_refused(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, _script())
    submission = await h.submit()

    with pytest.raises(WorkflowAlreadyStarted):
        await h.starter.start_claim(
            workflow_id=str(submission.claim_id),
            submission_id=submission.submission_id,
            request_type=RequestType.REIMBURSEMENT,
        )
    await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


class _FailingGateway:
    """A gateway whose provider has no API key: every call is a permanent failure."""

    async def structured(self, *args: Any, **kwargs: Any) -> Any:
        raise _no_key()

    async def vision(self, *args: Any, **kwargs: Any) -> Any:
        raise _no_key()


def _no_key() -> Exception:
    return ProviderPermanentError("no API key configured for provider openrouter")


# --- Registration and bill review (slice 3) ------------------------------------------------


async def test_registration_and_bill_review_open_their_checkpoints_with_the_records(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, _script())
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_registration")), "registration checkpoint"
        )
        [registration] = h.tasks.unfinished("confirm_registration")
        assert registration.role.value == "claims_operator"
        assert registration.payload["claim_number"].startswith("CLM-")
        assert [r["rule_id"] for r in registration.payload["rule_results"]] == [
            "R3", "R1", "R2", "R5", "R10",
        ]  # fmt: skip
        assert registration.payload["block_ref"] is not None
        assert h.tasks.unfinished("confirm_deductions") == []

        await h.complete("confirm_registration", {"action": "confirm"})
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_deductions")), "deductions checkpoint"
        )
        [deductions] = h.tasks.unfinished("confirm_deductions")
        assert deductions.role.value == "bill_processor"
        # A reimbursement: Sunrise's agreed discount (R24) binds cashless claims only, so the
        # lines are admitted as billed.
        assert deductions.payload["admissible_total"] == "48250.00"
        assert deductions.payload["payable"] == "45837.50"  # after the 5% co-pay
        assert len(h.money.blocks) == 1
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


async def test_a_person_edit_at_confirm_deductions_is_a_new_version_by_a_person(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, _script())
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        await h.complete("confirm_registration", {"action": "confirm"})
        edit = {"line_id": 4, "admissible": "20000", "reason": "Stool culture not advised"}
        task_id = await h.complete("confirm_deductions", {"action": "confirm", "lines": [edit]})
        await h.wait_for(lambda: bool(h.tasks.unfinished("approve")), "approve checkpoint")

        history = await StageRecordRepository(sessions).history(
            submission.claim_id, Stage.BILL_REVIEW
        )
        assert [r.created_by for r in history] == ["code", f"task:{task_id}"]
        line = next(ln for ln in history[-1].payload["lines"] if ln["line_id"] == 4)
        assert (line["judged_by"], line["admissible"]) == ("person", "20000.00")
        assert history[-1].payload["payable"] == "43937.50"
        assert "corrections_applied" in h.monitoring.actions(Stage.BILL_REVIEW)

        # A send-back from a later stage re-runs bill review and keeps the person's version.
        await h.complete(
            "approve", {"action": "send_back", "stage": "bill_review", "note": "Recheck line 4"}
        )
        await h.wait_for(lambda: bool(h.tasks.unfinished("confirm_deductions")), "deductions again")
        [again] = h.tasks.unfinished("confirm_deductions")
        assert again.payload["payable"] == "43937.50"
        assert h.monitoring.actions(Stage.BILL_REVIEW).count("stage_completed") == 2
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()


async def test_a_registration_override_is_stored_and_a_bad_one_reopens_the_task(
    env: WorkflowEnvironment, sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await _harness(env, sessions, _script())
    async with build_worker(env.client, TASK_QUEUE, claim_activities=h.activities):
        submission = await h.submit()
        await h.complete(
            "confirm_registration", {"overrides": {"member_id": "NOPE", "reason": "x"}}
        )
        await h.wait_for(
            lambda: "corrections_rejected" in h.monitoring.actions(Stage.REGISTRATION),
            "rejection event",
        )
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_registration")), "registration again"
        )
        [again] = h.tasks.unfinished("confirm_registration")
        assert "not on the policy" in again.payload["correction_error"]

        overrides = {"rules": {"R5": "Ported from another insurer; wait already served"}}
        task_id = await h.complete("confirm_registration", {"overrides": overrides})
        await h.wait_for(
            lambda: bool(h.tasks.unfinished("confirm_deductions")), "deductions checkpoint"
        )
        history = await StageRecordRepository(sessions).history(
            submission.claim_id, Stage.REGISTRATION
        )
        assert [r.created_by for r in history] == ["code", f"task:{task_id}"]
        r5 = next(r for r in history[-1].payload["rule_results"] if r["rule_id"] == "R5")
        assert r5["judged_by"] == "person"
        await env.client.get_workflow_handle(str(submission.claim_id)).terminate()
