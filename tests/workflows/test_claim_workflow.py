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

from app.services.adjudication.contract import Decision
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

TASK_QUEUE = "test-claims"
ALWAYS_ON = ["confirm_registration", "confirm_deductions", "approve", "release_payment"]
PERSON = str(uuid.uuid4())  # completes every task in these tests
DEFAULT_OUTCOMES = {
    "investigate": {"action": "cleared", "note": "Bills checked with the hospital"},
    "approve": {"action": "approve"},
    "crc": {"action": "uphold"},
    "release_payment": {"action": "release"},
}


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
    query: FakeQueryService
    settlement: FakeSettlementService
    classification: FakeClassificationService
    medical: FakeMedicalReviewService
    risk: FakeRiskService

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
        outcome = outcome or DEFAULT_OUTCOMES.get(kind, {"action": "confirm"})
        await handle.signal(
            ClaimWorkflow.task_completed, args=[str(task.id), {**outcome, "completed_by": PERSON}]
        )

    def opened_kinds(self) -> list[str]:
        return [
            e.detail["kind"] for e in self.monitoring.recorded if e.action == "checkpoint_opened"
        ]  # type: ignore[index]


async def _harness(
    activities_cls: type[ClaimActivities],
    *,
    adverse: bool = False,
    refer: bool = False,
    decision: Decision = "approve",
    classification: FakeClassificationService | None = None,
) -> AsyncIterator[Harness]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        monitoring, tasks = FakeMonitoring(), FakeTaskService()
        query, settlement = FakeQueryService(), FakeSettlementService(final=decision)
        classification = classification or FakeClassificationService()
        medical, risk = FakeMedicalReviewService(adverse=adverse), FakeRiskService(refer=refer)
        activities = activities_cls(
            monitoring,
            tasks,
            intake=FakeIntakeService(),
            classification=classification,
            digitisation=FakeDigitisationService(),
            registration=FakeRegistrationService(),
            bill_review=FakeBillReviewService(),
            medical_review=medical,
            risk=risk,
            adjudication=FakeAdjudicationService(decision),
            decision=FakeDecisionService(decision),
            communication=FakeCommunicationService(),
            query=query,
            settlement=settlement,
        )
        worker: Worker = build_worker(env.client, TASK_QUEUE, claim_activities=activities)
        async with worker:
            yield Harness(env, tasks, monitoring, query, settlement, classification, medical, risk)


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
    completed = [e.stage for e in harness.monitoring.recorded if e.action == "stage_completed"]
    assert completed == [s for s in Stage if s is not Stage.QUERY]


async def test_medical_coding_runs_before_bill_review_and_the_full_review_after_it(
    harness: Harness,
) -> None:
    """Bill review maps the verified procedures to a package; the full medical review reads
    that package's guideline. So: registration, coding, bill review, then the review."""
    handle = await harness.start()
    for kind in ALWAYS_ON:
        await harness.complete(handle, kind)
    await handle.result()

    completed = [e.stage for e in harness.monitoring.recorded if e.action == "stage_completed"]
    order = [Stage.REGISTRATION, Stage.MEDICAL_CODING, Stage.BILL_REVIEW, Stage.MEDICAL_REVIEW]
    assert [s for s in completed if s in order] == order
    assert harness.medical.coded and harness.medical.reviewed
    assert harness.medical.coded[0] < harness.medical.reviewed[0]


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
    for kind in ["confirm_deductions", "approve", "release_payment"]:
        await h.complete(handle, kind)
    result = await handle.result()

    assert result.status == "closed"
    assert {e.stage for e in h.monitoring.recorded if e.action == "stage_completed"} == set(Stage)
    # The reply re-ran classification and digitisation, then resumed at medical coding: new
    # documents can change the codes, so the package and the deductions, so the bill
    # processor confirms the sheet again.
    assert h.monitoring.actions(Stage.CLASSIFICATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.DIGITISATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.MEDICAL_CODING).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.BILL_REVIEW).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.MEDICAL_REVIEW).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.RISK).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.ADJUDICATION).count("stage_completed") == 2
    assert h.monitoring.actions(Stage.REGISTRATION).count("stage_completed") == 1


async def test_send_back_reruns_the_named_stage(harness: Harness) -> None:
    handle = await harness.start()
    await harness.complete(handle, "confirm_registration")
    await harness.complete(handle, "confirm_deductions")
    await harness.complete(
        handle, "approve", {"action": "send_back", "stage": "bill_review", "note": "recheck"}
    )
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


# --- P2-1: the medical officer's and the investigator's outcomes take effect -------------


async def test_an_overrule_writes_new_findings_through_the_service(
    adverse_harness: Harness,
) -> None:
    h = adverse_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    overrule = {"action": "overrule", "finding_ids": [1], "reason": "History is post-policy"}
    await h.complete(handle, "medical_findings", overrule)
    for kind in ["approve", "release_payment"]:
        await h.complete(handle, kind)

    assert (await handle.result()).status == "closed"
    [(ids, reason, actor)] = h.medical.overrules
    assert (ids, reason, actor) == ([1], "History is post-policy", f"user:{PERSON}")
    [event] = [e for e in h.monitoring.recorded if e.action == "findings_overruled"]
    assert event.detail == {"finding_ids": [1], "reason": "History is post-policy"}


async def test_an_overrule_without_finding_ids_reopens_the_medical_task(
    adverse_harness: Harness,
) -> None:
    h = adverse_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.complete(handle, "medical_findings", {"action": "overrule", "reason": "No ids"})
    await h.wait_for(lambda: bool(h.tasks.unfinished("medical_findings")), "reopened task")
    [task] = h.tasks.unfinished("medical_findings")

    assert "finding_ids" in task.payload["error"]
    assert h.medical.overrules == []
    await handle.terminate()


@pytest.mark.parametrize("verdict", ["cleared", "fraud_confirmed"])
async def test_the_investigator_verdict_is_recorded_through_the_service(
    referral_harness: Harness, verdict: str
) -> None:
    h = referral_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.complete(handle, "investigate", {"action": verdict, "note": " Forged pharmacy bill "})
    await h.wait_for(lambda: bool(h.tasks.unfinished("approve")), "approver task")

    assert h.risk.verdicts == [(verdict, "Forged pharmacy bill", f"user:{PERSON}")]
    # Adjudication runs after the verdict, so the decision rules see it.
    assert h.monitoring.actions(Stage.ADJUDICATION) == ["stage_completed"]
    await handle.terminate()


async def test_the_old_investigator_outcome_shape_reopens_the_task(
    referral_harness: Harness,
) -> None:
    h = referral_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    old = {"action": "confirm", "verdict": "genuine", "note": "Looks fine"}
    await h.complete(handle, "investigate", old)
    await h.wait_for(lambda: bool(h.tasks.unfinished("investigate")), "reopened task")
    [task] = h.tasks.unfinished("investigate")

    assert "cleared" in task.payload["error"] and "fraud_confirmed" in task.payload["error"]
    assert h.risk.verdicts == []
    await handle.terminate()


# --- Slice 5: decision, query loop and settlement order ------------------------------------


@pytest.fixture
async def partial_harness() -> AsyncIterator[Harness]:
    async for h in _harness(ClaimActivities, decision="partial"):
        yield h


@pytest.fixture
async def reject_harness() -> AsyncIterator[Harness]:
    async for h in _harness(ClaimActivities, decision="reject"):
        yield h


async def _to_query(h: Harness) -> WorkflowHandle[ClaimWorkflow, ClaimResult]:
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions"]:
        await h.complete(handle, kind)
    await h.wait_for(lambda: "stage_completed" in h.monitoring.actions(Stage.QUERY), "query")
    return handle


def _reminders(h: Harness) -> list[int]:
    return [
        e.detail["day"]  # type: ignore[index]
        for e in h.monitoring.recorded
        if e.action == "query_reminder"
    ]


async def test_query_reminders_at_day_3_and_7_then_a_reply_resumes_at_medical_coding(
    query_harness: Harness,
) -> None:
    h = query_harness
    handle = await _to_query(h)

    await h.env.sleep(timedelta(days=3, minutes=1))
    await h.wait_for(lambda: _reminders(h) == [3], "day 3 reminder")
    await h.env.sleep(timedelta(days=4))
    await h.wait_for(lambda: _reminders(h) == [3, 7], "day 7 reminder")
    assert h.monitoring.actions(Stage.MEDICAL_REVIEW).count("stage_completed") == 1

    reply = uuid.uuid4()
    await handle.signal(ClaimWorkflow.followup_received, str(reply))
    await h.complete(handle, "confirm_deductions")  # the sheet again, after the new coding
    await h.wait_for(lambda: bool(h.tasks.unfinished("approve")), "approve after the reply")

    assert ("reply", reply) in h.query.calls
    assert "query_answered" in h.monitoring.actions(Stage.QUERY)
    assert h.classification.classified[-1] == reply  # the reply's own documents
    assert h.monitoring.actions(Stage.MEDICAL_REVIEW).count("stage_completed") == 2
    assert "query_deadline_passed" not in h.monitoring.actions(Stage.QUERY)
    for kind in ["approve", "release_payment"]:
        await h.complete(handle, kind)
    assert (await handle.result()).status == "closed"


async def test_no_reply_by_the_deadline_opens_the_operator_task_and_waits_again(
    query_harness: Harness,
) -> None:
    h = query_harness
    handle = await _to_query(h)

    await h.env.sleep(timedelta(days=15, minutes=1))
    await h.wait_for(lambda: bool(h.tasks.unfinished("query_no_reply")), "operator task")
    [task] = h.tasks.unfinished("query_no_reply")

    assert task.role.value == "claims_operator"
    assert "query_deadline_passed" in h.monitoring.actions(Stage.QUERY)
    assert ("overdue", task.payload and h.query.calls[0][1]) in h.query.calls
    assert h.monitoring.actions(Stage.SETTLEMENT) == []  # never closed for a missing document

    await h.complete(handle, "query_no_reply", {"action": "extend"})
    await h.wait_for(lambda: "query_extended" in h.monitoring.actions(Stage.QUERY), "extended")
    await handle.signal(ClaimWorkflow.followup_received, str(uuid.uuid4()))
    for kind in ["confirm_deductions", "approve", "release_payment"]:
        await h.complete(handle, kind)
    assert (await handle.result()).status == "closed"
    assert _reminders(h) == [3, 7]  # no second round of reminders after the extension


async def test_a_partial_needs_the_crc_and_the_crc_can_send_it_back(
    partial_harness: Harness,
) -> None:
    h = partial_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions", "approve"]:
        await h.complete(handle, kind)
    await h.complete(
        handle, "crc", {"action": "send_back", "stage": "adjudication", "note": "cite clause"}
    )
    for kind in ["approve", "crc", "release_payment"]:
        await h.complete(handle, kind)

    assert (await handle.result()).draft == "partial"
    assert h.opened_kinds() == [
        "confirm_registration", "confirm_deductions", "approve", "crc", "approve", "crc",
        "release_payment",
    ]  # fmt: skip
    assert h.monitoring.actions(Stage.ADJUDICATION).count("stage_completed") == 2
    # The letter goes out only after the CRC upheld the partial.
    actions = [(e.stage, e.action) for e in h.monitoring.recorded]
    upheld = actions.index((Stage.DECISION, "stage_completed"))
    assert actions.index((Stage.COMMUNICATION, "stage_completed")) > upheld


async def test_payment_waits_for_the_finance_release(harness: Harness) -> None:
    handle = await harness.start()
    for kind in ["confirm_registration", "confirm_deductions", "approve"]:
        await harness.complete(handle, kind)
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("release_payment")), "release checkpoint"
    )

    assert harness.settlement.calls == [("check_payee", None)]  # nothing paid yet
    assert "payee_checked" in harness.monitoring.actions(Stage.SETTLEMENT)

    await harness.complete(handle, "release_payment")
    assert (await handle.result()).status == "closed"
    assert harness.settlement.calls == [("check_payee", None), ("settle", uuid.UUID(PERSON))]
    assert harness.monitoring.actions(Stage.SETTLEMENT)[-2:] == ["stage_completed", "claim_closed"]


async def test_a_release_without_the_release_action_reopens_the_finance_task(
    harness: Harness,
) -> None:
    handle = await harness.start()
    for kind in ["confirm_registration", "confirm_deductions", "approve"]:
        await harness.complete(handle, kind)
    await harness.complete(handle, "release_payment", {"action": "confirm"})
    await harness.wait_for(
        lambda: "outcome_refused" in harness.monitoring.actions(Stage.SETTLEMENT), "refusal"
    )
    await harness.wait_for(
        lambda: bool(harness.tasks.unfinished("release_payment")), "release task again"
    )
    [again] = harness.tasks.unfinished("release_payment")
    assert "release" in again.payload["error"]

    await harness.complete(handle, "release_payment")
    assert (await handle.result()).status == "closed"


async def test_a_rejection_skips_the_finance_release_and_closes(reject_harness: Harness) -> None:
    h = reject_harness
    handle = await h.start()
    for kind in ["confirm_registration", "confirm_deductions", "approve", "crc"]:
        await h.complete(handle, kind)

    result = await handle.result()

    assert (result.status, result.draft) == ("closed", "reject")
    assert "release_payment" not in h.opened_kinds()
    assert h.settlement.calls == [("check_payee", None), ("settle", None)]
