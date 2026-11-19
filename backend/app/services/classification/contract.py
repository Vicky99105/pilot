"""Contract for classification: label every page and group pages into documents.

Callers import this module only.
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import DocType

REVIEW_BELOW = 0.8  # a page labelled with lower confidence sends the set to an operator


class LabelledPage(BaseModel):
    page_id: UUID
    file_id: UUID
    seq: int  # 1-based position across the submission
    file_page: int
    slot: DocType | None
    image_key: str
    doc_type: DocType
    confidence: float
    quality_warning: str | None = None
    labelled_by: Literal["model", "person"] = "model"


class PageRange(BaseModel):
    start: int  # seq of the first page, inclusive
    end: int  # seq of the last page, inclusive


class ClassifiedDocument(BaseModel):
    document_id: UUID
    doc_type: DocType
    page_range: PageRange
    page_ids: list[UUID]  # in order; PageRef.page n is page_ids[n - 1]
    confidence: float  # the lowest page confidence


class Mismatch(BaseModel):
    kind: Literal["missing", "unexpected", "slot_mismatch"]
    doc_type: DocType
    detail: str
    page_ids: list[UUID] = []


class DocumentSet(BaseModel):
    claim_id: UUID
    submission_id: UUID
    pages: list[LabelledPage]
    documents: list[ClassifiedDocument]
    mismatches: list[Mismatch]
    needs_review: bool


class UnknownPage(PermanentError):
    """A correction names a page that is not in the claim's document set."""


class ClassificationService(Protocol):
    async def classify(self, submission_id: UUID) -> DocumentSet: ...

    async def apply_labels(
        self, claim_id: UUID, labels: dict[UUID, DocType], *, actor: str
    ) -> DocumentSet:
        """A person's labels, regrouped and stored as a new version."""
        ...
