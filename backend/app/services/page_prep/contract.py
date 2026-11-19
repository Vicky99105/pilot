"""Contract for page preparation: split each submitted file into page images.

Callers import this module only.
"""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import DocType

RENDER_DPI = 130


class PreparedPage(BaseModel):
    page_id: UUID
    file_id: UUID
    seq: int  # 1-based position across the whole submission, in upload order
    file_page: int  # 1-based page within its file
    slot: DocType | None  # the file's slot hint
    image_key: str  # FileStore key of the PNG rendering
    text_layer: str | None  # embedded PDF text; None for images and scans without text
    quality_warning: str | None  # recorded, never a reason to reject


class PagePreparer(Protocol):
    async def prepare(self, submission_id: UUID) -> list[PreparedPage]:
        """Idempotent: files already split are not rendered again."""
        ...
