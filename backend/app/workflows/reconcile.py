"""The reconciler. A Temporal Schedule runs ReconcileClaimsWorkflow every 5 minutes. Its
activity asks the intake service to start ClaimWorkflow for every claim still in status
"received" with no workflow, such as a claim saved while Temporal was down. The logic lives
in the intake service; Temporal only triggers it.
"""

from datetime import timedelta
from typing import Protocol

from temporalio import workflow
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.claim_activities import ClaimActivities

RECONCILE_SCHEDULE_ID = "reconcile-claims"
RECONCILE_EVERY = timedelta(minutes=5)
RECONCILE_TIMEOUT = timedelta(minutes=4)
RECONCILE_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["PermanentError"])


@workflow.defn
class ReconcileClaimsWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        """The claims whose workflow this run started (or found started), as IDs."""
        started: list[str] = await workflow.execute_activity_method(
            ClaimActivities.reconcile_claims,
            start_to_close_timeout=RECONCILE_TIMEOUT,
            retry_policy=RECONCILE_RETRY,
        )
        return started


class ScheduleClient(Protocol):
    """The part of temporalio.client.Client the schedule set-up uses."""

    async def create_schedule(self, id: str, schedule: Schedule) -> object: ...


async def ensure_reconcile_schedule(client: ScheduleClient, task_queue: str) -> bool:
    """Create the reconciler schedule unless it exists. Returns True when it was created."""
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "ReconcileClaimsWorkflow", id=RECONCILE_SCHEDULE_ID, task_queue=task_queue
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=RECONCILE_EVERY)]),
        # A run still going when the next is due makes the next one unnecessary.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await client.create_schedule(RECONCILE_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        return False
    return True
