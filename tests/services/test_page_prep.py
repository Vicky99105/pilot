import io
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.services.intake.contract import ClaimForm, SubmissionInput, UploadedFile
from app.services.intake.service import DefaultIntakeService
from app.services.page_prep.contract import PagePreparer
from app.services.page_prep.service import DefaultPagePreparer
from app.shared.seed_ids import MEMBER_USER_ID
from app.shared.types import DocType, RequestType
from tests.docs import make_image, make_pdf
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter

FORM = ClaimForm(
    policy_number="P-1",
    member_id="M-1",
    patient_name="Asha Verma",
    admission_date=date(2026, 11, 2),
    claimed_amount=Decimal("1000"),
)


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.files = FakeFileStore()
        self.intake = DefaultIntakeService(sessions, self.files, FakeWorkflowStarter())
        self.pages: PagePreparer = DefaultPagePreparer(sessions, self.files)

    async def submit(self, *uploads: tuple[DocType | None, bytes]) -> UUID:
        submission = await self.intake.accept(
            SubmissionInput(
                request_type=RequestType.REIMBURSEMENT,
                submitted_by=MEMBER_USER_ID,
                channel="member",
                form=FORM,
                files=[
                    UploadedFile(filename=f"f{i}", data=data, slot=slot)
                    for i, (slot, data) in enumerate(uploads)
                ],
            )
        )
        return submission.submission_id

    def png(self, key: str) -> Image.Image:
        data, _ = self.files.files[key]
        assert data.startswith(b"\x89PNG")
        return Image.open(io.BytesIO(data))


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


async def test_a_pdf_is_split_into_png_pages_with_its_text_layer(env: Env) -> None:
    pdf = make_pdf(["CLAIM FORM PART A\nPatient: Asha Verma", "FINAL BILL\nRoom rent 7500"])
    submission_id = await env.submit((None, pdf))

    pages = await env.pages.prepare(submission_id)

    assert [(p.seq, p.file_page) for p in pages] == [(1, 1), (2, 2)]
    assert pages[0].text_layer is not None and "Asha Verma" in pages[0].text_layer
    assert pages[1].text_layer is not None and "Room rent 7500" in pages[1].text_layer
    assert "\r" not in pages[0].text_layer
    width, height = env.png(pages[0].image_key).size
    assert (width, height) == pytest.approx((1075, 1520), abs=2)  # A4 at 130 DPI
    assert [p.quality_warning for p in pages] == [None, None]


async def test_pages_are_numbered_across_files_in_upload_order(env: Env) -> None:
    submission_id = await env.submit(
        (DocType.CLAIM_FORM_PART_A, make_pdf(["a1", "a2"])),
        (DocType.FINAL_BILL, make_image("JPEG")),
        (DocType.KYC, make_pdf(["k1"])),
    )

    pages = await env.pages.prepare(submission_id)

    assert [(p.seq, p.slot, p.file_page) for p in pages] == [
        (1, DocType.CLAIM_FORM_PART_A, 1),
        (2, DocType.CLAIM_FORM_PART_A, 2),
        (3, DocType.FINAL_BILL, 1),
        (4, DocType.KYC, 1),
    ]
    assert pages[2].text_layer is None


async def test_every_frame_of_a_tiff_is_a_page(env: Env) -> None:
    frames = [Image.new("RGB", (1240, 1754), c) for c in ("white", "gray")]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="TIFF", save_all=True, append_images=frames[1:], compression="tiff_deflate"
    )
    submission_id = await env.submit((DocType.FINAL_BILL, buffer.getvalue()))

    pages = await env.pages.prepare(submission_id)

    assert [p.file_page for p in pages] == [1, 2]


async def test_large_photos_are_scaled_down_to_the_render_size(env: Env) -> None:
    submission_id = await env.submit((DocType.FINAL_BILL, make_image("JPEG", (3000, 4000))))

    [page] = await env.pages.prepare(submission_id)

    assert max(env.png(page.image_key).size) <= 1600


async def test_quality_warnings_are_recorded_but_never_reject(env: Env) -> None:
    submission_id = await env.submit(
        (DocType.FINAL_BILL, make_image("JPEG", (400, 560))),
        (DocType.KYC, make_image("PNG", sharp=False)),
    )

    small, blank = await env.pages.prepare(submission_id)

    assert small.quality_warning is not None and "low resolution" in small.quality_warning
    assert blank.quality_warning is not None and "blurry" in blank.quality_warning


async def test_prepare_is_idempotent(env: Env) -> None:
    submission_id = await env.submit((None, make_pdf(["one", "two"])))

    first = await env.pages.prepare(submission_id)
    stored = len(env.files.files)
    second = await env.pages.prepare(submission_id)

    assert second == first
    assert len(env.files.files) == stored
