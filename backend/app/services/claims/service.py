"""Default claim queries on the claims database."""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, FileRow, PageRow, SubmissionRow
from app.services.claims.contract import (
    ClaimDetail,
    ClaimSummary,
    StoredFile,
    SubmissionView,
    SubmittedFileView,
)
from app.shared.types import DocType, RequestType


def _summary(claim: ClaimRow, first: SubmissionRow | None) -> ClaimSummary:
    return ClaimSummary(
        claim_id=claim.id,
        claim_number=claim.claim_number,
        request_type=RequestType(claim.request_type),
        status=claim.status,
        patient_name=(first.form or {}).get("patient_name") if first else None,
        deadline=claim.deadline,
        created_at=claim.created_at,
    )


class DefaultClaimQueryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list_for_submitter(self, user_id: UUID) -> list[ClaimSummary]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ClaimRow, SubmissionRow)
                .join(SubmissionRow, SubmissionRow.id == ClaimRow.submission_id)
                .where(SubmissionRow.submitted_by == user_id)
                .order_by(ClaimRow.created_at.desc())
            )
            return [_summary(claim, first) for claim, first in rows]

    async def get(self, claim_id: UUID) -> ClaimDetail | None:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
            if claim is None:
                return None
            submissions = list(
                await session.scalars(
                    select(SubmissionRow)
                    .where(SubmissionRow.claim_id == claim_id)
                    .order_by(SubmissionRow.created_at)
                )
            )
            files = list(
                await session.scalars(
                    select(FileRow)
                    .where(FileRow.submission_id.in_([s.id for s in submissions]))
                    .order_by(FileRow.position)
                )
            )
        first = next((s for s in submissions if s.id == claim.submission_id), None)
        return ClaimDetail(
            **_summary(claim, first).model_dump(),
            policy_number=claim.policy_number,
            member_id=claim.member_id,
            hospital_id=claim.hospital_id,
            submitted_by=first.submitted_by if first else None,
            submissions=[
                SubmissionView(
                    submission_id=s.id,
                    request_type=RequestType(s.request_type),
                    created_at=s.created_at,
                    duplicate_of=s.duplicate_of,
                    files=[
                        SubmittedFileView(
                            file_id=f.id,
                            filename=f.filename,
                            slot=DocType(f.slot_hint) if f.slot_hint else None,
                            mime=f.mime,
                            size_bytes=f.size_bytes,
                            storage_key=f.storage_key,
                            quality_warning=f.quality_warning,
                        )
                        for f in files
                        if f.submission_id == s.id
                    ],
                    missing_documents=[DocType(d) for d in s.missing_documents],
                    warnings=s.warnings,
                )
                for s in submissions
            ],
        )

    async def stored_file(self, storage_key: str) -> StoredFile | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(FileRow, PageRow.image_key, ClaimRow)
                    .outerjoin(PageRow, PageRow.file_id == FileRow.id)
                    .join(SubmissionRow, SubmissionRow.id == FileRow.submission_id)
                    .join(ClaimRow, ClaimRow.id == SubmissionRow.claim_id)
                    .where(
                        or_(FileRow.storage_key == storage_key, PageRow.image_key == storage_key)
                    )
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        file, _, claim = row
        return StoredFile(
            storage_key=storage_key,
            mime=file.mime if file.storage_key == storage_key else "image/png",
            claim_id=claim.id,
            policy_number=claim.policy_number,
            hospital_id=claim.hospital_id,
        )
