"""Contract for the query loop: what to ask the claimant, the query and its letter, the
reply, reminders and the deadline.

One query is open on a claim at a time. An item already answered in an earlier query is
never asked again: items match on document type, or on their words when either has no type.
A requested document type the claim already holds is never asked for. A query whose deadline
passes stays open as "overdue" for an operator: it is never closed automatically.
Callers import this module only.
"""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import DocType

QueryStatus = Literal["open", "overdue", "answered"]
ItemSource = Literal["missing_document", "medical", "adjudication"]


class QueryItem(BaseModel):
    text: str
    source: ItemSource
    doc_type: DocType | None = None  # the document asked for


class Query(BaseModel):
    query_id: UUID
    claim_id: UUID
    items: list[QueryItem]
    status: QueryStatus
    letter_id: UUID | None
    raised_at: datetime
    deadline: datetime
    reply_window_days: int
    reminder_days: list[int]  # days after raising when a reminder is due
    reminders: list[int]  # reminder days already sent
    reply_submission_id: UUID | None
    answered_at: datetime | None


class NothingToAsk(PermanentError):
    """Every item was asked and answered before; there is nothing new to ask."""


class QueryNotOpen(PermanentError):
    """The query is unknown, already answered, or on another claim."""


def same_item(a: str, b: str) -> bool:
    """Items match ignoring case and spacing."""
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


class QueryService(Protocol):
    async def outstanding_items(self, claim_id: UUID) -> list[QueryItem]:
        """Required documents not received in any submission, then the medical review's
        document requests (or, when both are empty, the adjudication draft's), less the
        document types the claim already holds."""
        ...

    async def raise_query(self, claim_id: UUID, items: list[QueryItem]) -> Query:
        """Open a query and issue its letter. Returns the open query if there is one."""
        ...

    async def attach_reply(self, query_id: UUID, submission_id: UUID) -> Query: ...

    async def record_reminder(self, query_id: UUID, day: int) -> Query: ...

    async def mark_overdue(self, query_id: UUID) -> Query: ...

    async def get(self, query_id: UUID) -> Query | None: ...

    async def open_query(self, claim_id: UUID) -> Query | None:
        """The claim's open or overdue query, if any."""
        ...
