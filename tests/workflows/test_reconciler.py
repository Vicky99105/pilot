"""The reconciler: a Temporal Schedule runs ReconcileClaimsWorkflow every 5 minutes, and its
activity starts ClaimWorkflow for claims saved while Temporal was down.

The workflow runs on the time-skipping server with the real intake service. That server has
no schedule API, so the schedule is checked against a fake of the client's create_schedule.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    WorkflowExecutionStatus,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from app.config import Settings
from app.database.models import ClaimRow
from app.database.seed import seed_claims_db
from app.services.intake.contract import ClaimForm, Submission, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService, DefaultWorkflowStarter
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import RequestType
from app.workflows.claim_activities import ClaimActivities
from app.workflows.reconcile import (
    RECONCILE_EVERY,
    RECONCILE_SCHEDULE_ID,
    ReconcileClaimsWorkflow,
    ensure_reconcile_schedule,
)
from app.workflows.worker import build_worker
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.stages import (
    FakeAdjudicationService,
    FakeBillReviewService,
    FakeCashlessService,
    FakeClassificationService,
    FakeCommunicationService,
    FakeDecisionService,
    FakeDigitisationService,
    FakeMedicalReviewService,
    FakeQueryService,
    FakeRegistrationService,
    FakeRiskService,
    FakeSettlementService,
)
from tests.fakes.tasks import FakeTaskService

TASK_QUEUE = "test-reconciler"


class SwitchableStarter(DefaultWorkflowStarter):
    """The real starter, which fails like an unreachable Temporal while down is True."""

    down = False

    async def start_claim(
        self, *, workflow_id: str, submission_id: UUID, request_type: RequestType
    ) -> None:
        if self.down:
            raise ConnectionError("temporal unreachable")
        await super().start_claim(
            workflow_id=workflow_id, submission_id=submission_id, request_type=request_type
        )


@dataclass
class Harness:
    env: WorkflowEnvironment
    sessions: async_sessionmaker[AsyncSession]
    starter: SwitchableStarter
    intake: DefaultIntakeService
    runs: int = 0

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
                    claimed_amount=Decimal("48250"),
                    declaration_accepted=True,
                ),
                files=[UploadedFile(filename="all.pdf", data=make_pdf(["PART A"]))],
            )
        )

    async def reconcile(self) -> list[str]:
        self.runs += 1
        return await self.env.client.execute_workflow(
            ReconcileClaimsWorkflow.run, id=f"reconcile-{self.runs}", task_queue=TASK_QUEUE
        )

    async def set_workflow_id(self, claim_id: UUID, workflow_id: str | None) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(ClaimRow, claim_id)
            assert row is not None
            row.workflow_id = workflow_id

    async def workflow_id(self, claim_id: UUID) -> str | None:
        async with self.sessions() as session:
            row = await session.get(ClaimRow, claim_id)
        assert row is not None
        return row.workflow_id


@pytest.fixture
async def harness(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    await seed_claims_db(sessions)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        starter = SwitchableStarter(Settings(temporal_task_queue=TASK_QUEUE), client=env.client)
        intake = DefaultIntakeService(sessions, FakeFileStore(), starter)
        activities = ClaimActivities(
            FakeMonitoring(),
            FakeTaskService(),
            intake=intake,
            classification=FakeClassificationService(),
            digitisation=FakeDigitisationService(),
            registration=FakeRegistrationService(),
            bill_review=FakeBillReviewService(),
            medical_review=FakeMedicalReviewService(),
            risk=FakeRiskService(),
            adjudication=FakeAdjudicationService(),
            decision=FakeDecisionService(),
            communication=FakeCommunicationService(),
            query=FakeQueryService(),
            settlement=FakeSettlementService(),
            cashless=FakeCashlessService(),
        )
        async with build_worker(env.client, TASK_QUEUE, claim_activities=activities):
            yield Harness(env, sessions, starter, intake)


async def test_the_reconciler_starts_a_claim_saved_while_temporal_was_down(
    harness: Harness,
) -> None:
    h = harness
    h.starter.down = True
    stranded = await h.submit()
    h.starter.down = False
    assert await h.workflow_id(stranded.claim_id) is None

    assert await h.reconcile() == [str(stranded.claim_id)]

    handle = h.env.client.get_workflow_handle(str(stranded.claim_id))
    described = await handle.describe()
    assert described.status == WorkflowExecutionStatus.RUNNING
    assert await h.workflow_id(stranded.claim_id) == str(stranded.claim_id)
    assert await h.reconcile() == []  # nothing left to start
    await handle.terminate()


async def test_a_duplicate_start_is_refused_and_ignored(harness: Harness) -> None:
    h = harness
    claim = await h.submit()  # started by intake
    first = await h.env.client.get_workflow_handle(str(claim.claim_id)).describe()
    await h.set_workflow_id(claim.claim_id, None)  # as if the ID never reached the row

    assert await h.reconcile() == [str(claim.claim_id)]

    again = await h.env.client.get_workflow_handle(str(claim.claim_id)).describe()
    assert again.run_id == first.run_id  # still the one workflow
    assert await h.workflow_id(claim.claim_id) == str(claim.claim_id)
    await h.env.client.get_workflow_handle(str(claim.claim_id)).terminate()


class FakeScheduleClient:
    """create_schedule as Temporal's client does it: a second create with the same ID is
    refused with ScheduleAlreadyRunningError."""

    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}

    async def create_schedule(self, id: str, schedule: Schedule, **_: Any) -> None:
        if id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules[id] = schedule


async def test_the_schedule_is_created_once_and_runs_every_five_minutes() -> None:
    client = FakeScheduleClient()

    assert await ensure_reconcile_schedule(client, "claims") is True
    assert await ensure_reconcile_schedule(client, "claims") is False

    [(schedule_id, schedule)] = client.schedules.items()
    assert schedule_id == RECONCILE_SCHEDULE_ID
    assert RECONCILE_EVERY == timedelta(minutes=5)
    assert [i.every for i in schedule.spec.intervals] == [RECONCILE_EVERY]
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert (action.workflow, action.task_queue) == ("ReconcileClaimsWorkflow", "claims")
