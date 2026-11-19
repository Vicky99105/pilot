"""Rendering and quality checks. Synchronous: callers run them in a worker thread.

pdfium is not thread-safe, so every pdfium call holds one process-wide lock.
"""

import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageSequence, ImageStat

from app.services.page_prep.contract import RENDER_DPI

PDFIUM_LOCK = threading.Lock()
MAX_IMAGE_SIDE = 1600  # photos are scaled to about the size of an A4 page at RENDER_DPI
MIN_SHORT_SIDE = 900  # pixels; below this, small print is hard to read
BLUR_THRESHOLD = 100.0  # edge-filter variance; blank or heavily blurred pages score lower
_EDGE_SAMPLE_SIDE = 1000


@dataclass(frozen=True)
class RenderedPage:
    png: bytes
    text: str | None
    quality_warning: str | None


def open_pdf(data: bytes) -> pdfium.PdfDocument:
    with PDFIUM_LOCK:
        return pdfium.PdfDocument(data)


def pdf_page_count(pdf: pdfium.PdfDocument) -> int:
    with PDFIUM_LOCK:
        return len(pdf)


def close_pdf(pdf: pdfium.PdfDocument) -> None:
    with PDFIUM_LOCK:
        pdf.close()


def render_pdf_page(pdf: pdfium.PdfDocument, index: int) -> RenderedPage:
    with PDFIUM_LOCK:
        page = pdf[index]
        try:
            image = page.render(scale=RENDER_DPI / 72).to_pil()
            textpage = page.get_textpage()
            # pdfium ends lines with \r\n; keep plain newlines.
            text = textpage.get_text_bounded().replace("\r\n", "\n").strip() or None
            textpage.close()
        finally:
            page.close()
    return RenderedPage(png=_png(image), text=text, quality_warning=quality_warning(image))


def image_frame_count(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as image:
        return getattr(image, "n_frames", 1)


def render_image_frame(data: bytes, index: int) -> RenderedPage:
    with Image.open(io.BytesIO(data)) as image:
        frame = next(_frames(image, index)).convert("RGB")
    warning = quality_warning(frame)
    frame.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    return RenderedPage(png=_png(frame), text=None, quality_warning=warning)


def _frames(image: Image.Image, index: int) -> Iterator[Image.Image]:
    for i, frame in enumerate(ImageSequence.Iterator(image)):
        if i == index:
            yield frame.copy()
            return


def quality_warning(image: Image.Image) -> str | None:
    """Resolution of the source and a blur score from the variance of an edge filter."""
    problems = []
    width, height = image.size
    if min(width, height) < MIN_SHORT_SIDE:
        problems.append(f"low resolution ({width}x{height} px)")
    if blur_score(image) < BLUR_THRESHOLD:
        problems.append("blurry or blank")
    return "; ".join(problems) or None


def blur_score(image: Image.Image) -> float:
    gray = image.convert("L")
    gray.thumbnail((_EDGE_SAMPLE_SIDE, _EDGE_SAMPLE_SIDE))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    # The filter marks the image border as an edge; leave it out.
    return float(ImageStat.Stat(edges.crop((2, 2, w - 2, h - 2))).var[0])


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
