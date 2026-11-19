"""Grouping and checklist comparison. Pure functions."""

import io
from itertools import groupby

from PIL import Image

from app.services.classification.contract import LabelledPage, Mismatch
from app.shared.types import DocType

CLASSIFY_IMAGE_SIDE = 1024


def small_jpeg(png: bytes) -> bytes:
    """A smaller copy for labelling; extraction uses the full rendering."""
    with Image.open(io.BytesIO(png)) as image:
        copy = image.convert("RGB")
    copy.thumbnail((CLASSIFY_IMAGE_SIDE, CLASSIFY_IMAGE_SIDE))
    out = io.BytesIO()
    copy.save(out, format="JPEG", quality=85)
    return out.getvalue()


def group_pages(pages: list[LabelledPage]) -> list[list[LabelledPage]]:
    """Consecutive pages of the same type in the same file form one document."""
    ordered = sorted(pages, key=lambda p: p.seq)
    return [list(run) for _, run in groupby(ordered, key=lambda p: (p.file_id, p.doc_type))]


def mismatches(
    pages: list[LabelledPage], required: list[DocType], listed: list[DocType]
) -> list[Mismatch]:
    found: list[Mismatch] = []
    for (slot, doc_type), run in groupby(
        sorted(pages, key=lambda p: p.seq), key=lambda p: (p.slot, p.doc_type)
    ):
        run_pages = list(run)
        if slot is not None and doc_type is not slot:
            found.append(
                Mismatch(
                    kind="slot_mismatch",
                    doc_type=doc_type,
                    detail=f"uploaded as {slot.value} but labelled {doc_type.value}",
                    page_ids=[p.page_id for p in run_pages],
                )
            )
    present = {p.doc_type for p in pages}
    for doc_type in sorted(present - set(listed), key=list(DocType).index):
        found.append(
            Mismatch(
                kind="unexpected",
                doc_type=doc_type,
                detail=f"{doc_type.value} is not on the checklist",
                page_ids=[p.page_id for p in pages if p.doc_type is doc_type],
            )
        )
    for doc_type in required:
        if doc_type not in present:
            found.append(
                Mismatch(kind="missing", doc_type=doc_type, detail=f"no {doc_type.value} found")
            )
    return found
