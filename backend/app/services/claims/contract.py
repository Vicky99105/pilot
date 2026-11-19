"""Contract for reading claims as the Submit area shows them. Read-only.

Callers import this module only.
"""

from datetime import datetime
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


class ClaimDetail(ClaimSummary):
    policy_number: str | None
    member_id: str | None
    hospital_id: str | None
    submitted_by: UUID | None
    closed_at: datetime | None = None
    submissions: list[SubmissionView]
    letters: list[LetterView] = []  # oldest first
    open_query: OpenQueryView | None = None


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

    async def stored_file(self, storage_key: str) -> StoredFile | None:
        """An uploaded file, a rendered page image or a letter PDF, by its storage key."""
        ...
