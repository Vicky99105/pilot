"""Model activities on the Temporal time-skipping server: a 429's retry delay is honoured,
the retry cap is per activity, and a quota used up for the day is not retried."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from itertools import pairwise
from typing import ClassVar
from uuid import UUID

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError

from app.services.classification.contract import DocumentSet
from app.services.model_gateway.contract import ModelQuotaExhausted, ModelUnavailable
from app.shared.types import Stage
from app.workflows.claim_activities import ClaimActivities
from app.workflows.claim_models import StageInput, StageResult
from app.workflows.claim_workflow import MODEL_RETRY
from tests.fakes.stages import FakeClassificationService
from tests.workflows.test_claim_workflow import Harness, _harness


class FlakyClassification(FakeClassificationService):
    """Raises the given error for the first `failures` calls, then classifies."""

    def __init__(self, error: Exception, failures: int) -> None:
        super().__init__()
        self.error, self.failures = error, failures

    async def classify(self, submission_id: UUID) -> DocumentSet:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return await super().classify(submission_id)


class AttemptLog(ClaimActivities):
    """Records each classification attempt and when it started, on the server's clock."""

    attempts: ClassVar[list[tuple[int, datetime]]] = []

    @activity.defn(name="classification")
    async def classification(self, inp: StageInput) -> StageResult:
        info = activity.info()
        type(self).attempts.append((info.attempt, info.started_time))
        return await super().classification(inp)


def _gaps() -> list[float]:
    times = [t for _, t in AttemptLog.attempts]
    return [(b - a).total_seconds() for a, b in pairwise(times)]


async def _flaky(error: Exception, failures: int) -> AsyncIterator[Harness]:
    AttemptLog.attempts = []
    async for h in _harness(AttemptLog, classification=FlakyClassification(error, failures)):
        yield h


@pytest.fixture
async def rate_limited_twice() -> AsyncIterator[Harness]:
    async for h in _flaky(ModelUnavailable("HTTP 429", retry_after_seconds=30), 2):
        yield h


@pytest.fixture
async def always_unavailable() -> AsyncIterator[Harness]:
    async for h in _flaky(ModelUnavailable("HTTP 503"), 1000):
        yield h


@pytest.fixture
async def quota_used_up() -> AsyncIterator[Harness]:
    async for h in _flaky(ModelQuotaExhausted("the quota per day is used up"), 1000):
        yield h


def test_model_retry_policy_matches_the_spec() -> None:
    assert MODEL_RETRY.initial_interval == timedelta(seconds=10)
    assert MODEL_RETRY.backoff_coefficient == 2
    assert MODEL_RETRY.maximum_interval == timedelta(minutes=5)
    assert MODEL_RETRY.maximum_attempts == 8
    assert MODEL_RETRY.non_retryable_error_types == ["PermanentError"]


async def test_the_provider_retry_delay_sets_the_wait_before_the_next_attempt(
    rate_limited_twice: Harness,
) -> None:
    h = rate_limited_twice
    handle = await h.start()
    await h.env.sleep(timedelta(minutes=2))  # skips time past both waits
    await h.wait_for(
        lambda: bool(h.tasks.unfinished("confirm_registration")), "registration checkpoint"
    )

    assert [a for a, _ in AttemptLog.attempts] == [1, 2, 3]
    # 30 s as the provider asked, not the policy's 10 s and 20 s.
    assert [round(g) for g in _gaps()] == [30, 30]
    await handle.terminate()


async def test_the_workflow_fails_cleanly_after_eight_attempts(
    always_unavailable: Harness,
) -> None:
    h = always_unavailable
    handle = await h.start()

    with pytest.raises(WorkflowFailureError):
        await handle.result()

    assert [a for a, _ in AttemptLog.attempts] == list(range(1, 9))
    assert [round(g) for g in _gaps()] == [10, 20, 40, 80, 160, 300, 300]
    failed = [e for e in h.monitoring.recorded if e.action == "stage_failed"]
    assert [e.stage for e in failed] == [Stage.CLASSIFICATION]
    assert "HTTP 503" in str(failed[0].detail)


async def test_a_quota_used_up_for_the_day_is_not_retried(quota_used_up: Harness) -> None:
    h = quota_used_up
    handle = await h.start()

    with pytest.raises(WorkflowFailureError):
        await handle.result()

    assert [a for a, _ in AttemptLog.attempts] == [1]
    failed = [e for e in h.monitoring.recorded if e.action == "stage_failed"]
    assert "per day" in str(failed[0].detail)
