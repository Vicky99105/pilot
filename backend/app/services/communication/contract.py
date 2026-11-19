"""Contract for communication: letters to the claimant as PDFs, sent through the
NotificationService contract.

Kinds: approval, partial (with the deduction table), rejection (reasons with clause refs),
query (the items to send) and authorisation (Part D style, for an approved or partly
approved cashless request: the amount approved now, the earlier total and the new total).
Every number in a letter comes from code: the deduction sheet, the decision record and the
authorisation. One FAST_TEXT model call may rewrite the reasons in plain language;
when the rewrite changes or drops any number or clause reference, the raw reasons are used.

Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError

LetterKind = Literal["approval", "partial", "rejection", "query", "authorisation"]


class DeductionRow(BaseModel):
    line_id: int | None  # the bill line; None for the co-pay and approver adjustments
    description: str
    claimed: Decimal
    deducted: Decimal
    reason: str
    clause_ref: str | None


class Letter(BaseModel):
    letter_id: UUID
    claim_id: UUID
    claim_number: str | None
    kind: LetterKind
    recipient: str | None  # email address; None when none is on file
    addressee: str  # the patient's name
    decision_version: int | None  # the DecisionRecord version; None for a query letter
    query_id: UUID | None
    amount: Decimal  # the amount payable; zero for a rejection or query
    claimed: Decimal | None
    deductions: list[DeductionRow]
    raw_reasons: list[str]  # built by code
    reasons: list[str]  # as printed: the plain-language rewrite, or the raw reasons
    plain_language: bool
    query_items: list[str]
    reply_by: datetime | None  # query letters
    request_type: str | None = None  # authorisation letters: preauth, enhancement, discharge
    previous_total: Decimal | None = None  # authorisation letters: approved earlier
    approved_total: Decimal | None = None  # authorisation letters: approved in all
    pdf_key: str
    sent: bool
    send_error: str | None = None
    created_at: datetime


class NotReadyForLetter(PermanentError):
    """No final decision yet, or a partial or reject the CRC has not upheld."""


class CommunicationService(Protocol):
    async def issue_letter(self, claim_id: UUID) -> Letter:
        """The decision letter for the latest DecisionRecord. Idempotent per record version."""
        ...

    async def issue_query_letter(self, claim_id: UUID, query_id: UUID) -> Letter:
        """The letter for a query. Idempotent per query."""
        ...
