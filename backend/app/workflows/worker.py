"""Temporal worker that runs inside the FastAPI process."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import Settings
from app.workflows.ping import PingWorkflow, ping

logger = logging.getLogger(__name__)

# Caps memory use on the 5.4 GB server (see the build plan).
MAX_CONCURRENT_ACTIVITIES = 2
MAX_BACKOFF_SECONDS = 30.0


def build_worker(client: Client, task_queue: str) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[PingWorkflow],
        activities=[ping],
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
    )


def next_backoff(attempt: int) -> float:
    """Seconds to wait before reconnect attempt number `attempt` (0-based)."""
    return min(2.0**attempt, MAX_BACKOFF_SECONDS)


async def run_worker_forever(settings: Settings) -> None:
    """Connect and run the worker; on failure, retry with backoff until cancelled."""
    attempt = 0
    while True:
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            logger.info("Temporal worker connected to %s", settings.temporal_address)
            attempt = 0
            await build_worker(client, settings.temporal_task_queue).run()
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = next_backoff(attempt)
            logger.warning("Temporal worker unavailable; retrying in %.0fs", delay, exc_info=True)
            attempt += 1
            await asyncio.sleep(delay)
