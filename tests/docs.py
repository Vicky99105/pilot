"""Generated test documents: small PDFs with a text layer, and images in each format."""

import io

import pillow_heif
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def make_pdf(pages: list[str]) -> bytes:
    """One A4 page per string; each page draws its text, so the PDF has a text layer."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=True)  # same text, same bytes
    for text in pages:
        y = 800
        for line in text.splitlines() or [""]:
            pdf.drawString(60, y, line)
            y -= 18
        # A table-like grid gives the page enough edges to look sharp, like a real form.
        for row in range(20):
            pdf.line(60, 500 - row * 18, 540, 500 - row * 18)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def make_image(fmt: str, size: tuple[int, int] = (1240, 1754), *, sharp: bool = True) -> bytes:
    """An image with text-like strokes. sharp=False gives a flat page with no edges."""
    image = Image.new("RGB", size, "white")
    if sharp:
        draw = ImageDraw.Draw(image)
        for y in range(40, size[1] - 40, 24):
            for x in range(40, size[0] - 80, 60):
                draw.text((x, y), "Rs 1,250", fill="black")
    buffer = io.BytesIO()
    if fmt == "HEIC":
        pillow_heif.register_heif_opener()
        image.save(buffer, format="HEIF")
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()
