"""File checks for intake: type sniffing from the bytes, size limits and HEIC conversion."""

import hashlib
import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image

from app.services.intake.contract import MAX_FILE_BYTES, MB, Channel, UploadedFile
from app.shared.types import DocType

pillow_heif.register_heif_opener()

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
HEIC = "image/heic"


def sniff(data: bytes) -> str | None:
    """The MIME type the bytes really are, or None when the type is not accepted."""
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return HEIC
    return None


def heic_to_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


@dataclass(frozen=True)
class CheckedFile:
    filename: str
    slot: DocType | None
    sha256: str  # of the uploaded bytes
    size_bytes: int
    mime: str  # of the bytes to store
    data: bytes  # the bytes to store (converted for HEIC)
    warning: str | None = None


def check_file(upload: UploadedFile, channel: Channel) -> CheckedFile | str:
    """A file ready to store, or the reason it is rejected."""
    name, data = upload.filename, upload.data
    limit = MAX_FILE_BYTES[channel]
    if len(data) > limit:
        return f"{name}: larger than the {limit // MB} MB limit"
    mime = sniff(data)
    if mime is None:
        return f"{name}: unsupported file type; upload PDF, JPG, PNG, TIFF or HEIC"
    stored, warning = data, None
    if mime == HEIC:
        try:
            stored = heic_to_jpeg(data)
        except Exception:
            return f"{name}: HEIC image could not be read; upload a JPG"
        mime, warning = "image/jpeg", f"{name}: HEIC photo converted to JPEG"
    return CheckedFile(
        filename=name,
        slot=upload.slot,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mime=mime,
        data=stored,
        warning=warning,
    )


def files_digest(hashes: list[str]) -> str:
    """One hash for a set of files, independent of upload order."""
    return hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()
