"""Query through its contract: what to ask, the query letter, replies and reminders."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import SubmissionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.communication.service import DefaultCommunicationService
from app.services.query.contract import (
    NothingToAsk,
    QueryItem,
    QueryNotOpen,
    QueryService,
)
from app.services.query.service import DefaultQueryService
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import DocType, RequestType, Stage
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.notifications import FakeNotificationService
from tests.services.claim_builders import ClaimSpec, add_digitised_claim

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
RECEIPT = QueryItem(
    text="Payment receipt", source="missing_document", doc_type=DocType.PAYMENT_RECEIPT
)
HISTOPATHOLOGY = QueryItem(text="Histopathology report of the appendix", source="medical")


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.records = StageRecordRepository(sessions)
        self.files = FakeFileStore()
        self.notifications = FakeNotificationService()
        self.communication = DefaultCommunicationService(
            sessions, self.files, FakeModelGateway({}), self.notifications, now=lambda: NOW
        )

    @property
    def service(self) -> QueryService:
        return DefaultQueryService(
            self.sessions, self.communication, reply_days=15, reminder_days=[3, 7], now=lambda: NOW
        )

    async def claim(
        self, missing: list[str], received: list[list[str]], medical: list[str]
    ) -> UUID:
        claim_id = await add_digitised_claim(self.sessions, ClaimSpec(submitted_by=MEMBER_USER_ID))
        intake = await self.records.latest(claim_id, Stage.INTAKE)
        assert intake is not None
        await self.records.append(
            claim_id,
            Stage.INTAKE,
            {**intake.payload, "missing_documents": missing},
            created_by="test",
        )
        for doc_types in received:  # one classification version per submission
            documents = [{"doc_type": d} for d in doc_types]
            await self.records.append(
                claim_id, Stage.CLASSIFICATION, {"documents": documents}, created_by="test"
            )
        await self.records.append(
            claim_id, Stage.MEDICAL_REVIEW, {"query_items": medical}, created_by="test"
        )
        return claim_id

    async def reply(self, claim_id: UUID) -> UUID:
        submission = SubmissionRow(
            id=uuid4(),
            request_type=RequestType.QUERY_REPLY.value,
            parent_claim_id=claim_id,
            claim_id=claim_id,
            form={},
            created_at=NOW,
        )
        async with self.sessions.begin() as session:
            session.add(submission)
        return submission.id


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


async def test_outstanding_items_are_missing_documents_and_medical_query_items(env: Env) -> None:
    claim_id = await env.claim(
        missing=["payment_receipt", "claim_form_part_b"],
        received=[["claim_form_part_a", "final_bill"], ["claim_form_part_b"]],
        medical=["Histopathology report of the appendix"],
    )

    items = await env.service.outstanding_items(claim_id)

    assert items == [RECEIPT, HISTOPATHOLOGY]


async def test_raising_a_query_issues_a_query_letter_with_the_items(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])

    query = await env.service.raise_query(claim_id, [RECEIPT, HISTOPATHOLOGY])

    assert query.status == "open"
    assert (query.raised_at, query.deadline) == (NOW, NOW + timedelta(days=15))
    assert (query.reply_window_days, query.reminder_days) == (15, [3, 7])
    assert query.letter_id is not None
    [sent] = env.notifications.sent
    assert sent.to == "asha.verma@member.example"  # the submitter's address
    letters = await env.records.history(claim_id, Stage.COMMUNICATION)
    [letter] = [r.payload for r in letters]
    assert (letter["kind"], letter["query_id"]) == ("query", str(query.query_id))
    assert letter["query_items"] == [RECEIPT.text, HISTOPATHOLOGY.text]
    history = await env.records.history(claim_id, Stage.QUERY)
    assert [r.payload["status"] for r in history] == ["open"]


async def test_a_second_raise_while_one_is_open_returns_the_open_query(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])

    first = await env.service.raise_query(claim_id, [RECEIPT])
    again = await env.service.raise_query(claim_id, [RECEIPT, HISTOPATHOLOGY])

    assert again.query_id == first.query_id
    assert len(env.notifications.sent) == 1


async def test_an_answered_item_is_never_asked_again(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])
    first = await env.service.raise_query(claim_id, [RECEIPT, HISTOPATHOLOGY])
    await env.service.attach_reply(first.query_id, await env.reply(claim_id))

    with pytest.raises(NothingToAsk):
        await env.service.raise_query(claim_id, [HISTOPATHOLOGY])
    lab = QueryItem(text="Liver function test", source="medical")
    second = await env.service.raise_query(claim_id, [HISTOPATHOLOGY, lab])

    assert second.query_id != first.query_id
    assert [i.text for i in second.items] == ["Liver function test"]


async def test_a_reply_answers_the_query_once(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])
    query = await env.service.raise_query(claim_id, [RECEIPT])
    submission_id = await env.reply(claim_id)

    answered = await env.service.attach_reply(query.query_id, submission_id)
    again = await env.service.attach_reply(query.query_id, submission_id)

    assert (answered.status, answered.reply_submission_id) == ("answered", submission_id)
    assert again == answered
    with pytest.raises(QueryNotOpen):
        await env.service.attach_reply(query.query_id, await env.reply(claim_id))
    assert await env.service.open_query(claim_id) is None


async def test_a_reply_from_another_claim_is_refused(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])
    other = await env.claim(missing=[], received=[[]], medical=[])
    query = await env.service.raise_query(claim_id, [RECEIPT])

    with pytest.raises(QueryNotOpen, match="not on claim"):
        await env.service.attach_reply(query.query_id, await env.reply(other))


async def test_reminders_and_the_deadline_are_recorded(env: Env) -> None:
    claim_id = await env.claim(missing=["payment_receipt"], received=[[]], medical=[])
    query = await env.service.raise_query(claim_id, [RECEIPT])

    await env.service.record_reminder(query.query_id, 3)
    await env.service.record_reminder(query.query_id, 3)  # a retried activity
    await env.service.record_reminder(query.query_id, 7)
    overdue = await env.service.mark_overdue(query.query_id)

    assert (overdue.status, overdue.reminders) == ("overdue", [3, 7])
    assert await env.service.open_query(claim_id) == overdue  # still awaiting the claimant
    history: list[dict[str, Any]] = [
        r.payload for r in await env.records.history(claim_id, Stage.QUERY)
    ]
    assert [(p["status"], p["reminders"]) for p in history] == [
        ("open", []),
        ("open", [3]),
        ("open", [3, 7]),
        ("overdue", [3, 7]),
    ]
