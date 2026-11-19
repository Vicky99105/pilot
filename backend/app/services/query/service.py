"""Default query service on the queries table. Every change is also appended to
stage_records (stage query), so the claim keeps the full history."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import QueryRow, SubmissionRow, utcnow
from app.database.repositories.stage_records import StageRecordRepository, append_in_session
from app.services.communication.contract import CommunicationService
from app.services.query.contract import (
    ItemSource,
    NothingToAsk,
    Query,
    QueryItem,
    QueryNotOpen,
    same_item,
)
from app.shared.types import DocType, Stage

PENDING = ("open", "overdue")


def _label(doc_type: DocType) -> str:
    return doc_type.value.replace("_", " ").capitalize()


def _same(a: QueryItem, b: QueryItem) -> bool:
    """The same document type, or the same words when either item has no type."""
    if a.doc_type is not None and b.doc_type is not None:
        return a.doc_type == b.doc_type
    return same_item(a.text, b.text)


def _requested(
    payload: dict[str, Any] | None, source: ItemSource, received: set[Any]
) -> list[QueryItem]:
    """A stage record's document requests, less the document types the claim holds."""
    return [
        QueryItem(text=r["text"], source=source, doc_type=DocType(r["doc_type"]))
        for r in (payload or {}).get("query_items", [])
        if r["doc_type"] not in received
    ]


class DefaultQueryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        communication: CommunicationService,
        *,
        reply_window: timedelta,
        reminders: list[timedelta],
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._communication = communication
        self._reply_window = reply_window
        self._reminders = sorted(r for r in reminders if timedelta(0) < r < reply_window)
        self._records = StageRecordRepository(sessions)
        self._now = now

    async def outstanding_items(self, claim_id: UUID) -> list[QueryItem]:
        intake = await self._records.latest(claim_id, Stage.INTAKE)
        received = {
            d.get("doc_type")
            for record in await self._records.history(claim_id, Stage.CLASSIFICATION)
            for d in record.payload.get("documents", [])
        }
        missing = intake.payload.get("missing_documents", []) if intake else []
        items = [
            QueryItem(text=_label(DocType(d)), source="missing_document", doc_type=DocType(d))
            for d in missing
            if d not in received
        ]
        medical = await self._records.latest(claim_id, Stage.MEDICAL_REVIEW)
        items += _requested(medical.payload if medical else None, "medical", received)
        if not items:
            draft = await self._records.latest(claim_id, Stage.ADJUDICATION)
            if draft is not None and draft.payload.get("decision") == "query":
                items = _requested(draft.payload, "adjudication", received)
        return items

    async def raise_query(self, claim_id: UUID, items: list[QueryItem]) -> Query:
        pending = await self.open_query(claim_id)
        if pending is not None:
            return pending if pending.letter_id else await self._issue_letter(pending.query_id)
        answered = await self._answered_items(claim_id)
        new: list[QueryItem] = []
        for item in items:
            if not any(_same(item, a) for a in [*answered, *new]):
                new.append(item)
        if not new:
            raise NothingToAsk(f"claim {claim_id}: every item was asked and answered before")
        now = self._now()
        row = QueryRow(
            id=uuid4(),
            claim_id=claim_id,
            items=[i.model_dump(mode="json") for i in new],
            status="open",
            deadline=now + self._reply_window,
            reminders=[],
            raised_at=now,
        )
        async with self._sessions.begin() as session:
            session.add(row)
        return await self._issue_letter(row.id)

    async def _issue_letter(self, query_id: UUID) -> Query:
        """The query letter, then the query's first stage record, which names the letter."""
        row = await self._get_row(query_id)
        letter = await self._communication.issue_query_letter(row.claim_id, query_id)
        return await self._change(query_id, letter_id=letter.letter_id)

    async def attach_reply(self, query_id: UUID, submission_id: UUID) -> Query:
        async with self._sessions() as session:
            row = await session.get(QueryRow, query_id)
            submission = await session.get(SubmissionRow, submission_id)
        if row is None:
            raise QueryNotOpen(f"query {query_id} not found")
        if row.status == "answered" and row.reply_submission_id == submission_id:
            return self._to_query(row)
        if row.status not in PENDING:
            raise QueryNotOpen(f"query {query_id} is {row.status}")
        if submission is None or submission.claim_id != row.claim_id:
            raise QueryNotOpen(f"submission {submission_id} is not on claim {row.claim_id}")
        return await self._change(
            query_id, status="answered", reply_submission_id=submission_id, answered_at=self._now()
        )

    async def record_reminder(self, query_id: UUID, number: int) -> Query:
        current = await self._get_row(query_id)
        if number in current.reminders:
            return self._to_query(current)
        return await self._change(query_id, reminders=sorted([*current.reminders, number]))

    async def mark_overdue(self, query_id: UUID) -> Query:
        current = await self._get_row(query_id)
        if current.status != "open":
            return self._to_query(current)
        return await self._change(query_id, status="overdue")

    async def get(self, query_id: UUID) -> Query | None:
        async with self._sessions() as session:
            row = await session.get(QueryRow, query_id)
        return self._to_query(row) if row else None

    async def open_query(self, claim_id: UUID) -> Query | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(QueryRow)
                .where(QueryRow.claim_id == claim_id, QueryRow.status.in_(PENDING))
                .order_by(QueryRow.raised_at.desc())
                .limit(1)
            )
        return self._to_query(row) if row else None

    async def _answered_items(self, claim_id: UUID) -> list[QueryItem]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(QueryRow).where(QueryRow.claim_id == claim_id, QueryRow.status == "answered")
            )
            return [QueryItem.model_validate(item) for row in rows for item in row.items]

    async def _get_row(self, query_id: UUID) -> QueryRow:
        async with self._sessions() as session:
            row = await session.get(QueryRow, query_id)
        if row is None:
            raise QueryNotOpen(f"query {query_id} not found")
        return row

    async def _change(self, query_id: UUID, **values: Any) -> Query:
        async with self._sessions.begin() as session:
            row = await session.get(QueryRow, query_id, with_for_update=True)
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)
            await session.flush()
            await self._append(session, row)
            return self._to_query(row)

    async def _append(self, session: AsyncSession, row: QueryRow) -> None:
        await append_in_session(
            session,
            row.claim_id,
            Stage.QUERY,
            self._to_query(row).model_dump(mode="json"),
            created_by="code",
        )

    def _to_query(self, row: QueryRow) -> Query:
        return Query(
            query_id=row.id,
            claim_id=row.claim_id,
            items=[QueryItem.model_validate(i) for i in row.items],
            status=row.status,
            letter_id=row.letter_id,
            raised_at=row.raised_at,
            deadline=row.deadline,
            reply_window=self._reply_window,
            reminder_offsets=self._reminders,
            reminders=list(row.reminders),
            reply_submission_id=row.reply_submission_id,
            answered_at=row.answered_at,
        )
