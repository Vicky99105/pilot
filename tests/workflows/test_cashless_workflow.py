"""The cashless claim workflow on the Temporal time-skipping test server: one workflow per
admission through the pre-auth, an enhancement, the discharge or a cancellation, with a clock
per request."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.shared.types import RequestType
from app.workflows.claim_activities import ClaimActivities
from app.workflows.claim_models import ClaimInput, ClaimResult
from app.workflows.claim_workflow import ClaimWorkflow
from app.workflows.worker import build_worker
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.stages import (
    FakeAdjudicationService,
    FakeBillReviewService,
    FakeCashlessService,
    FakeClassificationService,
    FakeCommunicationService,
    FakeDecisionService,
    FakeDigitisationService,
    FakeIntakeService,
    FakeMedicalReviewService,
    FakeQueryService,
    FakeRegistrationService,
    FakeRiskService,
    FakeSettlementService,
)
from tests.fakes.tasks import FakeTaskService

TASK_QUEUE = "test-cashless"
PERSON = str(uuid.uuid4())
OUTCOMES = {"approve": {"action": "approve"}, "release_payment": {"action": "release"}}
REVIEW = ["confirm_registration", "confirm_deductions", "approve"]


@dataclass
class Harness:
    env: WorkflowEnvironment
    tasks: FakeTaskService
    monitoring: FakeMonitoring
    cashless: FakeCashlessService
    settlement: FakeSettlementService
    classification: FakeClassificationService

    async def start(self) -> WorkflowHandle[ClaimWorkflow, ClaimResult]:
        return await self.env.client.start_workflow(
            ClaimWorkflow.run,
            ClaimInput(submission_id=uuid.uuid4(), request_type=RequestType.PREAUTH),
            id=f"claim-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )

    async def wait_for(self, check: Callable[[], bool], what: str) -> None:
        for _ in range(300):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    async def complete(self, handle: WorkflowHandle[ClaimWorkflow, ClaimResult], kind: str) -> Any:
        await self.wait_for(lambda: bool(self.tasks.unfinished(kind)), f"task {kind}")
        [task] = self.tasks.unfinished(kind)
        self.tasks.tasks[task.id] = task.model_copy(update={"status": "done"})
        outcome = OUTCOMES.get(kind, {"action": "confirm"})
        await handle.signal(
            ClaimWorkflow.task_completed, args=[str(task.id), {**outcome, "completed_by": PERSON}]
        )
        return task

    async def review(self, handle: WorkflowHandle[ClaimWorkflow, ClaimResult]) -> None:
        for kind in REVIEW:
            await self.complete(handle, kind)

    async def send(
        self, handle: WorkflowHandle[ClaimWorkflow, ClaimResult], request: RequestType
    ) -> None:
        submission_id = uuid.uuid4()
        self.cashless.requests[submission_id] = request
        await handle.signal(ClaimWorkflow.followup_received, args=[str(submission_id)])

    def actions(self, action: str) -> list[dict[str, Any]]:
        return [e.detail or {} for e in self.monitoring.recorded if e.action == action]


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        monitoring, tasks = FakeMonitoring(), FakeTaskService()
        cashless, settlement = FakeCashlessService(), FakeSettlementService()
        classification = FakeClassificationService()
        activities = ClaimActivities(
            monitoring,
            tasks,
            intake=FakeIntakeService(),
            classification=classification,
            digitisation=FakeDigitisationService(),
            registration=FakeRegistrationService(),
            bill_review=FakeBillReviewService(),
            medical_review=FakeMedicalReviewService(),
            risk=FakeRiskService(),
            adjudication=FakeAdjudicationService(),
            decision=FakeDecisionService(),
            communication=FakeCommunicationService(),
            query=FakeQueryService(),
            settlement=settlement,
            cashless=cashless,
        )
        async with build_worker(env.client, TASK_QUEUE, claim_activities=activities):
            yield Harness(env, tasks, monitoring, cashless, settlement, classification)


async def test_preauth_then_enhancement_then_discharge_closes_the_claim(h: Harness) -> None:
    handle = await h.start()

    await h.review(handle)
    await h.wait_for(lambda: ("awaiting_discharge", "preauth") in h.cashless.calls, "wait")
    await h.send(handle, RequestType.ENHANCEMENT)
    await h.review(handle)
    await h.wait_for(lambda: ("awaiting_discharge", "enhancement") in h.cashless.calls, "wait")
    assert h.settlement.calls == []  # nothing is paid before the discharge
    await h.send(handle, RequestType.DISCHARGE)
    await h.review(handle)
    await h.complete(handle, "release_payment")
    result = await handle.result()

    assert result.status == "closed"
    assert h.cashless.calls == [
        ("begin_request", "preauth"),
        ("authorise", "preauth"),
        ("awaiting_discharge", "preauth"),
        ("begin_request", "enhancement"),
        ("authorise", "enhancement"),
        ("awaiting_discharge", "enhancement"),
        ("begin_request", "discharge"),
        ("authorise", "discharge"),
    ]
    assert [c for c, _ in h.settlement.calls] == ["check_payee", "settle"]
    # Each follow-up is classified from its own submission, after the pre-auth's.
    assert len(h.classification.classified) == 3
    # The authorisation stage runs before the letter, for every request.
    stages = [e.stage.value for e in h.monitoring.recorded if e.action == "stage_completed"]
    assert stages.count("authorisation") == 3 and stages.count("communication") == 3
    assert stages.index("authorisation") < stages.index("communication")
    assert h.actions("clock_breached") == []


async def test_a_cancellation_releases_and_closes_without_settlement(h: Harness) -> None:
    handle = await h.start()
    await h.review(handle)
    await h.wait_for(lambda: ("awaiting_discharge", "preauth") in h.cashless.calls, "wait")

    await h.send(handle, RequestType.CANCELLATION)
    result = await handle.result()

    assert result.status == "closed"
    assert h.cashless.calls[-1] == ("cancel", "cancellation")
    assert h.settlement.calls == []


async def test_checkpoint_tasks_take_the_requests_clock_as_their_deadline(h: Harness) -> None:
    """The queues order tasks by deadline, so a cashless task carries its request's clock:
    one hour for the pre-auth, not the four-hour checkpoint SLA."""
    handle = await h.start()

    task = await h.complete(handle, "confirm_registration")

    assert task.deadline == h.cashless.clocks[0]
    await h.complete(handle, "confirm_deductions")
    await h.complete(handle, "approve")
    await handle.terminate()


async def test_a_missed_clock_is_recorded_and_nothing_is_approved_for_it(h: Harness) -> None:
    """The pre-auth's hour passes with the approver task open: a clock_breached event is
    recorded, the task stays open, and the claim goes on only when a person approves."""
    handle = await h.start()
    await h.complete(handle, "confirm_registration")
    await h.complete(handle, "confirm_deductions")
    await h.wait_for(lambda: bool(h.tasks.unfinished("approve")), "the approver task")

    await h.env.sleep(timedelta(hours=1, minutes=5))
    await h.wait_for(lambda: bool(h.actions("clock_breached")), "the breach")

    [breach] = h.actions("clock_breached")
    assert breach["request_type"] == "preauth"
    assert h.tasks.unfinished("approve")  # still waiting for a person
    assert ("authorise", "preauth") not in h.cashless.calls
    await h.complete(handle, "approve")
    await h.wait_for(lambda: ("awaiting_discharge", "preauth") in h.cashless.calls, "wait")
    await handle.terminate()
