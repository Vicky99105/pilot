import uuid

from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from app.workflows.ping import PingWorkflow, ping
from app.workflows.worker import build_worker, next_backoff


async def test_ping_activity_returns_pong() -> None:
    result = await ActivityEnvironment().run(ping, "claims")

    assert result == "pong: claims"


async def test_ping_workflow_runs_on_the_app_worker() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(env.client, task_queue="test-claims")
        async with worker:
            result = await env.client.execute_workflow(
                PingWorkflow.run,
                "claims",
                id=f"ping-{uuid.uuid4()}",
                task_queue="test-claims",
            )

    assert result == "pong: claims"


def test_next_backoff_doubles_and_caps_at_30_seconds() -> None:
    assert [next_backoff(n) for n in range(7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
