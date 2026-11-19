"""Default page preparation: PDFs through pdfium, images through Pillow, one page at a time
so memory stays bounded by one rendered page plus the source file."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import FileRow, PageRow
from app.services.file_store.contract import FileStore
from app.services.page_prep import _helpers as render
from app.services.page_prep.contract import PreparedPage
from app.shared.types import DocType


class DefaultPagePreparer:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], file_store: FileStore) -> None:
        self._sessions = sessions
        self._files = file_store

    async def prepare(self, submission_id: UUID) -> list[PreparedPage]:
        async with self._sessions() as session:
            files = list(
                await session.scalars(
                    select(FileRow)
                    .where(FileRow.submission_id == submission_id)
                    .order_by(FileRow.position)
                )
            )
            split = set(
                await session.scalars(
                    select(PageRow.file_id).where(PageRow.file_id.in_([f.id for f in files]))
                )
            )
        for file in files:
            if file.id not in split:
                await self._split(file)
        return await self._load(files)

    async def _split(self, file: FileRow) -> None:
        """Render every page, then write the file's page rows in one transaction, so a
        file has either all its pages or none."""
        data = await self._files.get(file.storage_key)
        rows = []
        async for number, rendered in _rendered_pages(data, file.mime):
            rows.append(
                PageRow(
                    id=uuid4(),
                    file_id=file.id,
                    page_number=number,
                    image_key=await self._files.put(rendered.png, content_type="image/png"),
                    text_layer=rendered.text,
                    quality_warning=rendered.quality_warning,
                )
            )
        warnings = [f"page {r.page_number}: {r.quality_warning}" for r in rows if r.quality_warning]
        async with self._sessions.begin() as session:
            session.add_all(rows)
            stored = await session.get(FileRow, file.id)
            assert stored is not None
            stored.quality_warning = "; ".join(warnings) or None

    async def _load(self, files: list[FileRow]) -> list[PreparedPage]:
        slots = {f.id: DocType(f.slot_hint) if f.slot_hint else None for f in files}
        order = {f.id: f.position for f in files}
        async with self._sessions() as session:
            rows = list(await session.scalars(select(PageRow).where(PageRow.file_id.in_(order))))
        rows.sort(key=lambda r: (order[r.file_id], r.page_number))
        return [
            PreparedPage(
                page_id=r.id,
                file_id=r.file_id,
                seq=seq,
                file_page=r.page_number,
                slot=slots[r.file_id],
                image_key=r.image_key or "",
                text_layer=r.text_layer,
                quality_warning=r.quality_warning,
            )
            for seq, r in enumerate(rows, start=1)
        ]


async def _rendered_pages(data: bytes, mime: str) -> AsyncIterator[tuple[int, render.RenderedPage]]:
    """(1-based page number, page), rendered one at a time in a worker thread."""
    if mime == "application/pdf":
        pdf = await asyncio.to_thread(render.open_pdf, data)
        try:
            for i in range(await asyncio.to_thread(render.pdf_page_count, pdf)):
                yield i + 1, await asyncio.to_thread(render.render_pdf_page, pdf, i)
        finally:
            await asyncio.to_thread(render.close_pdf, pdf)
        return
    for i in range(await asyncio.to_thread(render.image_frame_count, data)):
        yield i + 1, await asyncio.to_thread(render.render_image_frame, data, i)
