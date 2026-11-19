"""Reconsideration through its contract, with the real intake service and a fake workflow
starter: a closed claim reopens with its history, and a new workflow run starts on the new
evidence."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.intake.contract import UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.reconsideration.contract import NotReconsiderable, ReconsiderationService
from app.services.reconsideration.service import DefaultReconsiderationService
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import DocType, RequestType, Stage
from tests.docs import make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.monitoring import FakeMonitoring
from tests.services.claim_builders import ClaimSpec, add_digitised_claim

D = Decimal
CLOSED_AT = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
GROUNDS = "Nursing was ICU nursing billed by the hour, not ward nursing."


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.starter = FakeWorkflowStarter()
        self.monitoring = FakeMonitoring()
        intake = DefaultIntakeService(
            sessions, FakeFileStore(), self.starter, signaller=self.starter
        )
        self.service: ReconsiderationService = DefaultReconsiderationService(
            sessions, intake, self.monitoring
        )

    async def closed_claim(self, *, paid: str = "60644.52", status: str = "closed") -> UUID:
        claim_id = await add_digitised_claim(self.sessions, ClaimSpec())
        records = StageRecordRepository(self.sessions)
        await records.append(claim_id, Stage.DECISION,
                             {"final_decision": "partial", "final_amount": paid},
                             created_by="test")  # fmt: skip
        await records.append(claim_id, Stage.SETTLEMENT,
                             {"status": "settled", "paid_amount": paid},
                             created_by="test")  # fmt: skip
        async with self.sessions.begin() as session:
            row = await session.get(ClaimRow, claim_id)
            assert row is not None
            row.status, row.closed_at, row.workflow_id = status, CLOSED_AT, str(claim_id)
        return claim_id

    def evidence(self) -> list[UploadedFile]:
        return [UploadedFile(filename="icu-note.pdf", data=make_pdf(["ICU NURSING NOTE"]),
                             slot=DocType.OTHER)]  # fmt: skip


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


async def test_a_closed_claim_reopens_and_a_new_run_starts_on_the_new_evidence(
    env: Env,
) -> None:
    claim_id = await env.closed_claim()

    done = await env.service.request(
        claim_id, grounds=f"  {GROUNDS}  ", submitted_by=MEMBER_USER_ID, channel="member",
        files=env.evidence(),
    )  # fmt: skip

    assert (done.number, done.grounds, done.workflow_id) == (
        1,
        GROUNDS,
        f"{claim_id}:reconsideration:1",
    )
    assert (done.previous_decision, done.previous_amount, done.previously_paid) == (
        "partial",
        D("60644.52"),
        D("60644.52"),
    )
    assert done.previous_closed_at == CLOSED_AT
    assert env.starter.started == {
        done.workflow_id: (done.submission_id, RequestType.RECONSIDERATION)
    }
    async with env.sessions() as session:
        claim = await session.get(ClaimRow, claim_id)
        submission = await session.get(SubmissionRow, done.submission_id)
    assert claim is not None and submission is not None
    assert (claim.status, claim.closed_at, claim.workflow_id) == (
        "reconsideration",
        None,
        done.workflow_id,
    )
    assert (submission.claim_id, submission.request_type) == (claim_id, "reconsideration")
    [event] = env.monitoring.recorded
    assert (event.action, event.stage.value, event.actor) == (
        "reconsideration_requested",
        "intake",
        f"user:{MEMBER_USER_ID}",
    )
    assert event.detail is not None and event.detail["grounds"] == GROUNDS
    assert await env.service.history(claim_id) == [done]
    # The history is intact: the earlier decision and settlement are still there.
    records = StageRecordRepository(env.sessions)
    assert len(await records.history(claim_id, Stage.SETTLEMENT)) == 1


async def test_a_second_reconsideration_after_the_first_closes_is_number_two(env: Env) -> None:
    claim_id = await env.closed_claim()
    first = await env.service.request(claim_id, grounds=GROUNDS, submitted_by=MEMBER_USER_ID,
                                      channel="member", files=env.evidence())  # fmt: skip
    async with env.sessions.begin() as session:
        row = await session.get(ClaimRow, claim_id)
        assert row is not None
        row.status, row.closed_at = "closed", CLOSED_AT

    second = await env.service.request(claim_id, grounds=GROUNDS, submitted_by=MEMBER_USER_ID,
                                       channel="member", files=env.evidence())  # fmt: skip

    assert (first.number, second.number) == (1, 2)
    assert second.workflow_id == f"{claim_id}:reconsideration:2"


@pytest.mark.parametrize(
    ("status", "grounds", "with_files", "match"),
    [
        ("bill_review", GROUNDS, True, "not closed"),
        ("closed", "  too short ", True, "grounds"),
        ("closed", GROUNDS, False, "evidence"),
    ],
    ids=["open_claim", "short_grounds", "no_evidence"],
)
async def test_a_reconsideration_needs_a_closed_claim_grounds_and_evidence(
    env: Env, status: str, grounds: str, with_files: bool, match: str
) -> None:
    claim_id = await env.closed_claim(status=status)

    with pytest.raises(NotReconsiderable, match=match):
        await env.service.request(
            claim_id, grounds=grounds, submitted_by=MEMBER_USER_ID, channel="member",
            files=env.evidence() if with_files else [],
        )  # fmt: skip
    assert env.starter.started == {}
    async with env.sessions() as session:
        row = await session.get(ClaimRow, claim_id)
    assert row is not None and row.status == status  # nothing reopened
