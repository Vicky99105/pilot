"""Fakes of the stage contracts for workflow tests: clean results, no checkpoints."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.classification.contract import DocumentSet
from app.services.digitisation.contract import ClaimRecord
from app.services.intake.contract import ClaimForm, Submission
from app.shared.types import DocType, RequestType

_NAMESPACE = uuid.UUID("6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0001")


def claim_id_for(submission_id: UUID) -> UUID:
    return uuid.uuid5(_NAMESPACE, str(submission_id))


class FakeIntakeService:
    """Every submission ID is a known submission whose claim ID derives from it."""

    async def accept(self, submission: Any) -> Submission:
        raise NotImplementedError("workflow tests start claims directly")

    async def get(self, submission_id: UUID) -> Submission | None:
        return Submission(
            submission_id=submission_id,
            claim_id=claim_id_for(submission_id),
            request_type=RequestType.REIMBURSEMENT,
            parent_claim_id=None,
            form=ClaimForm(
                policy_number="P-1",
                member_id="M-1",
                patient_name="A",
                admission_date=date(2026, 11, 2),
                claimed_amount=Decimal("1000"),
            ),
            files=[],
            missing_documents=[],
            warnings=[],
            deadline=None,
            created_at=datetime(2026, 11, 19, tzinfo=UTC),
        )


class FakeClassificationService:
    def __init__(self) -> None:
        self.classified: list[UUID] = []

    async def classify(self, submission_id: UUID) -> DocumentSet:
        self.classified.append(submission_id)
        return DocumentSet(
            claim_id=claim_id_for(submission_id),
            submission_id=submission_id,
            pages=[],
            documents=[],
            mismatches=[],
            needs_review=False,
        )

    async def apply_labels(
        self, claim_id: UUID, labels: dict[UUID, DocType], *, actor: str
    ) -> DocumentSet:
        raise NotImplementedError


class FakeDigitisationService:
    async def extract(self, claim_id: UUID) -> ClaimRecord:
        return ClaimRecord(claim_id=claim_id, classification_version=1)

    async def apply_corrections(
        self, claim_id: UUID, fields: dict[str, Any], *, actor: str
    ) -> ClaimRecord:
        raise NotImplementedError
