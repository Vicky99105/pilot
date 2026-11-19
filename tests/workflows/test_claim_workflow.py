"""ClaimWorkflow skeleton on the Temporal time-skipping test server."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.shared.types import RequestType, Stage
from app.workflows.claim_activities import ClaimActivities
from app.workflows.claim_models import ClaimInput, ClaimResult, StageInput, StageResult
from app.workflows.claim_workflow import CHECKPOINT_SLA, ClaimWorkflow
from app.workflows.worker import build_worker
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.stages import (
    FakeAdjudicationService,
    FakeBillReviewService,
    FakeClassificationService,
    FakeDigitisationService,
    FakeIntakeService,
    FakeMedicalReviewService,
    FakeRegistrationService,
    FakeRiskService,
)
from tests.fakes.tasks import FakeTaskService

TASK_QUEUE = "test-claims"
ALWAYS_ON = ["confirm_registration", "confirm_deductions", "approve", "release_payment"]


class QueryOnceActivities(ClaimActivities):
    """Adjudication drafts a query the first time, then approves."""

    adjudications = 0

    @activity.defn(name="adjudication")
    async def adjudication(self, inp: StageInput) -> StageResult:
        result = await super().adjudication(inp)
        type(self).adjudications += 1
        draft = "query" if type(self).adjudications == 1 else "approve"
        return result.model_copy(update={"draft": draft})


@dataclass
class Harness:
    env: WorkflowEnvironment
    tasks: FakeTaskService
    monitoring: FakeMonitoring

    async def start(self) -> WorkflowHandle[ClaimWorkflow, ClaimResult]:
        return await self.env.client.start_workflow(
            ClaimWorkflow.run,
            ClaimInput(submission_id=uuid.uuid4(), request_type=RequestType.REIMBURSEMENT),
            id=f"claim-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )

    async def wait_for(self, check: Callable[[], bool], what: str) -> None:
        for _ in range(200):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    async def complete(
        self,
        handle: WorkflowHandle[ClaimWorkflow, ClaimResult],
        kind: str,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        await self.wait_for(lambda: bool(self.tasks.unfinished(kind)), f"task {kind}")
        [task] = self.tasks.unfinished(kind)
        self.tasks.tasks[task.id] = task.model_copy(update={"status": "done"})
        await handle.signal(
            ClaimWorkflow.task_completed, args=[str(task.id), outcome or {"action": "confirm"}]
        )

    def opened_kinds(self) -> list[str]:
        return [e.detail["kind"] for e in self.monitoring.events if e.action == "checkpoint_opened"]  # type: ignore[index]


async def _harness(
    activities_cls: type[ClaimActivities], *, adverse: bool = False, refer: bool = False
) -> AsyncIterator[Harness]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        monitoring, tasks = FakeMonitoring(), FakeTaskService()
        activities = activities_cls(
            monitoring,
            tasks,
            intake=FakeIntakeService(),
            classification=FakeClassificationService(),
            digitisation=FakeDigitisationService(),
            registration=FakeRegistrationService(),
            bill_review=FakeBillReviewService(),
            medical_review=FakeMedicalReviewService(adverse=adverse),
            risk=FakeRiskService(refer=refer),
            adjudication=FakeAdjudicationService(),
        )
        worker: Worker = build_worker(env.client, TASK_QUEUE, claim_activities=activities)
        async with worker:
            yield Harness(env, tasks, monitoring)


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for h in _harness(ClaimActivities):
        yield h


@pytest.fixture
async def adverse_harness() -> AsyncIterator[Harness]:
    async for h in _harness(ClaimActivities, adverse=True):
        yield h


@pytest.fixture
async def referral_harness() -> AsyncIterator[Harness]:
    async for h in _harness(ClaimActivities, refer=True):
        yield h


@pytest.fixture
async def query_harness() -> AsyncIterator[Harness]:
    QueryOnceActivities.adjudications = 0
    async for h in _harness(QueryOnceActivities):
        yield h


async def test_happy_path_opens_always_on_checkpoints_and_closes(harness: Harness) -> None:
    handle = await harness.start()
    for kind in ALWAYS_ON:
        await harness.complete(handle, kind)

    result = await handle.result()

    assert result.status == "closed"
    assert harness.opened_kinds() == ALWAYS_ON
    completed = [e.stage for e in harness.monitoring.events if e.action == "stage_completed"]
    assert completed == [s for s in Stage if s is not Stage.QUERY]


async def test_waits_at_checkpoint_until_task_completed_arrives(harness: Harness) -> None:
    handle = await harness.start()
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("confirm_registration")), "registration task"
    )
    await asyncio.sleep(0.5)

    assert harness.monitoring.actions(Stage.BILL_REVIEW) == []

    await harness.complete(handle, "confirm_registration")
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("confirm_deductions")), "bill review checkpoint"
    )
    assert harness.monitoring.actions(Stage.BILL_REVIEW)[0] == "stage_completed"
    await handle.terminate()


async def test_records_escalation_when_the_deadline_passes_and_keeps_waiting(
    harness: Harness,
) -> None:
    handle = await harness.start()
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("confirm_registration")), "registration task"
    )

    await harness.env.sleep(CHECKPOINT_SLA + timedelta(minutes=1))
    await harness.wait_for(
        lambda: "checkpoint_escalated" in harness.monitoring.actions(Stage.REGISTRATION),
        "escalation event",
    )

    assert harness.monitoring.actions(Stage.BILL_REVIEW) == []
    for kind in ALWAYS_ON:
        await harness.complete(handle, kind)
    assert (await handle.result()).status == "closed"


async def test_query_path_passes_all_13_stages(query_harness: Harness) -> None:
    h = query_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.wait_for(lambda: "stage_completed" in h.monitoring.actions(Stage.QUERY), "query")

    await handle.signal(ClaimWorkflow.followup_received, str(uuid.uuid4()))
    for kind in ["approve", "release_payment"]:
        await h.complete(handle, kind)
    result = await handle.result()

    assert result.status == "closed"
    assert {e.stage for e in h.monitoring.events if e.action == "stage_completed"} == set(Stage)
    # The reply re-ran classification and digitisation, then resumed at adjudication.
    assert h.monitoring.actions(Stage.CLASSIFICATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.DIGITISATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.ADJUDICATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.REGISTRATION).count("stage_completed") == 1


async def test_send_back_reruns_the_named_stage(harness: Harness) -> None:
    handle = await harness.start()
    await harness.complete(handle, "confirm_registration")
    await harness.complete(handle, "confirm_deductions")
    await harness.complete(handle, "approve", {"action": "send_back", "stage": "bill_review"})
    for kind in ["confirm_deductions", "approve", "release_payment"]:
        await harness.complete(handle, kind)

    assert (await handle.result()).status == "closed"
    assert harness.monitoring.actions(Stage.BILL_REVIEW).count("stage_completed") == 2
    assert harness.monitoring.actions(Stage.REGISTRATION).count("stage_completed") == 1
    assert "sent_back" in harness.monitoring.actions(Stage.DECISION)


async def test_hold_pauses_before_the_next_stage_until_resume(harness: Harness) -> None:
    handle = await harness.start()
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("confirm_registration")), "registration task"
    )
    await handle.signal(ClaimWorkflow.hold)
    await harness.complete(handle, "confirm_registration")
    await asyncio.sleep(0.5)

    assert harness.monitoring.actions(Stage.BILL_REVIEW) == []

    await handle.signal(ClaimWorkflow.resume)
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("confirm_deductions")), "bill review after resume"
    )
    await handle.terminate()


async def test_adverse_medical_findings_open_the_medical_officer_task(
    adverse_harness: Harness,
) -> None:
    h = adverse_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.wait_for(lambda: bool(h.tasks.unfinished("medical_findings")), "medical task")
    [task] = h.tasks.unfinished("medical_findings")

    assert task.role.value == "medical_officer"
    assert task.payload["adverse"] is True
    assert task.payload["findings"][0]["kind"] == "pre_existing"
    assert h.monitoring.actions(Stage.RISK) == []  # waits for the medical officer

    for kind in ["medical_findings", "approve", "release_payment"]:
        await h.complete(handle, kind)
    assert (await handle.result()).status == "closed"
    assert "investigate" not in h.opened_kinds()


async def test_a_referral_opens_the_investigator_task(referral_harness: Harness) -> None:
    h = referral_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.wait_for(lambda: bool(h.tasks.unfinished("investigate")), "investigator task")
    [task] = h.tasks.unfinished("investigate")

    assert task.role.value == "investigator"
    assert (task.payload["score"], task.payload["refer"]) == (60, True)
    assert h.monitoring.actions(Stage.ADJUDICATION) == []  # waits for the investigator

    for kind in ["investigate", "approve", "release_payment"]:
        await h.complete(handle, kind)
    assert (await handle.result()).status == "closed"
    assert "medical_findings" not in h.opened_kinds()
