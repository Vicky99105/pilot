"""Trivial workflow that proves the worker is wired to Temporal."""

from datetime import timedelta

from temporalio import activity, workflow


@activity.defn
async def ping(name: str) -> str:
    return f"pong: {name}"


@workflow.defn
class PingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            ping, name, start_to_close_timeout=timedelta(seconds=10)
        )
