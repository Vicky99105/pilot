"""Contract for reading claims as the Submit area shows them. Read-only.

Callers import this module only.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import DocType, RequestType


class ClaimSummary(BaseModel):
    claim_id: UUID
    claim_number: str | None
    request_type: RequestType
    status: str
    patient_name: str | None
    deadline: datetime | None
    created_at: datetime


class SubmittedFileView(BaseModel):
    file_id: UUID
    filename: str | None
    slot: DocType | None
    mime: str
    size_bytes: int
    storage_key: str
    quality_warning: str | None


class SubmissionView(BaseModel):
    submission_id: UUID
    request_type: RequestType
    created_at: datetime
    duplicate_of: UUID | None
    files: list[SubmittedFileView]
    missing_documents: list[DocType]
    warnings: list[str]


class LetterView(BaseModel):
    letter_id: UUID
    kind: str  # approval, partial, rejection, query
    created_at: datetime
    pdf_key: str
    download_url: str  # GET /api/files/{pdf_key}, permission checked
    sent: bool


class OpenQueryView(BaseModel):
    query_id: UUID
    items: list[str]
    status: str  # open, or overdue once the deadline has passed
    raised_at: datetime
    deadline: datetime
    reply_url: str  # POST, multipart


class AuthorisationView(BaseModel):
    """One cashless authorisation: what the request added and the total approved."""

    request_type: RequestType
    status: str  # awaiting_discharge, final, rejected or cancelled
    approved_now: Decimal
    approved_total: Decimal
    authorised_at: datetime


class ReconsiderationView(BaseModel):
    number: int
    grounds: str
    requested_at: datetime
    previous_decision: str | None
    previous_amount: Decimal
    previously_paid: Decimal


class ChecklistItem(BaseModel):
    doc_type: DocType
    required: bool
    mandatory_from_amount: Decimal | None
    notes: str | None


class ClaimDetail(ClaimSummary):
    policy_number: str | None
    member_id: str | None
    hospital_id: str | None
    submitted_by: UUID | None
    closed_at: datetime | None = None
    submissions: list[SubmissionView]
    letters: list[LetterView] = []  # oldest first
    open_query: OpenQueryView | None = None
    # Cashless claims: the request being processed and its clock, the authorisations so far
    # (oldest first), and the hospital's originals (due 15 days after discharge).
    current_request: RequestType | None = None
    clock_deadline: datetime | None = None  # None once the current request is authorised
    authorisations: list[AuthorisationView] = []
    originals_due_at: datetime | None = None
    originals_received_at: datetime | None = None
    # A supervisor's hold, while it lasts; and the claimant's reconsiderations, oldest first.
    held_at: datetime | None = None
    hold_reason: str | None = None
    reconsiderations: list[ReconsiderationView] = []


class ClaimPage(BaseModel):
    """One page of the claim's latest classified document set, for the page viewer."""

    page_id: UUID
    seq: int  # 1-based position across the submission
    doc_type: DocType
    confidence: float
    image_key: str  # GET /api/files/{image_key}, permission checked
    quality_warning: str | None
    document_id: UUID | None  # None when the page belongs to no document
    page_in_document: int | None  # what PageRef.page means for this page


class ClaimPageDocument(BaseModel):
    document_id: UUID
    doc_type: DocType
    page_ids: list[UUID]  # in order; PageRef.page n is page_ids[n - 1]


class ClaimPages(BaseModel):
    claim_id: UUID
    classification_version: int
    pages: list[ClaimPage]
    documents: list[ClaimPageDocument]


class StoredFile(BaseModel):
    """What a permission check needs to know about a stored file or page image."""

    storage_key: str
    mime: str
    claim_id: UUID
    policy_number: str | None
    hospital_id: str | None


class ClaimQueryService(Protocol):
    async def list_for_submitter(self, user_id: UUID) -> list[ClaimSummary]: ...

    async def get(self, claim_id: UUID) -> ClaimDetail | None: ...

    async def pages(self, claim_id: UUID) -> ClaimPages | None:
        """The latest classified pages, or None before classification."""
        ...

    async def stored_file(self, storage_key: str) -> StoredFile | None:
        """An uploaded file, a rendered page image or a letter PDF, by its storage key."""
        ...

    async def checklist(self, request_type: RequestType) -> list[ChecklistItem]:
        """The documents a request needs, in checklist order."""
        ...
