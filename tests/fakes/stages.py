"""Fakes of the stage contracts for workflow tests: clean results, no checkpoints."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.adjudication.contract import Decision, DraftDecision
from app.services.bill_review.contract import DeductionSheet, LineEdit
from app.services.classification.contract import DocumentSet
from app.services.digitisation.contract import ClaimRecord
from app.services.intake.contract import ClaimForm, Submission
from app.services.medical_review.contract import Citation, Finding, MedicalFindings
from app.services.registration.contract import (
    MatchedHospital,
    MatchedMember,
    RegistrationOverrides,
    RegistrationResult,
)
from app.services.risk.contract import RiskHit, RiskReport
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


class FakeRegistrationService:
    async def register(self, claim_id: UUID) -> RegistrationResult:
        return RegistrationResult(
            claim_id=claim_id,
            digitisation_version=1,
            claim_number="CLM-2026-000001",
            member=MatchedMember(
                match="exact_id",
                member_id="M-1",
                name="A",
                dob=date(1990, 1, 1),
                relationship="self",
                reason="exact member ID M-1",
            ),
            policy=None,
            hospital=MatchedHospital(
                match="none", rohini_id=None, name=None, city=None, differential_billing=True
            ),
            admission_date=date(2026, 11, 2),
            accident=False,
            block_amount=Decimal("1000.00"),
            rule_results=[],
            duplicates=[],
            block_ref=None,
        )

    async def apply_overrides(
        self, claim_id: UUID, overrides: RegistrationOverrides, *, actor: str
    ) -> RegistrationResult:
        raise NotImplementedError


class FakeBillReviewService:
    async def review(self, claim_id: UUID) -> DeductionSheet:
        zero = Decimal("0.00")
        return DeductionSheet(
            claim_id=claim_id,
            digitisation_version=1,
            registration_version=1,
            product_uin="UIN",
            lines=[],
            rule_results=[],
            claimed_total=zero,
            admissible_total=zero,
            co_pay=zero,
            payable=zero,
            flagged_lines=[],
        )

    async def apply_edits(
        self, claim_id: UUID, edits: list[LineEdit], *, actor: str
    ) -> DeductionSheet:
        raise NotImplementedError


class FakeMedicalReviewService:
    """Clean findings, or one adverse finding when adverse=True."""

    def __init__(self, *, adverse: bool = False) -> None:
        self.adverse = adverse

    async def review(self, claim_id: UUID) -> MedicalFindings:
        findings = (
            [
                Finding(
                    kind="pre_existing",
                    text="history predates the policy",
                    severity="adverse",
                    citations=[Citation(ref="clause:Excl01")],
                    judged_by="code",
                )
            ]
            if self.adverse
            else []
        )
        return MedicalFindings(
            claim_id=claim_id,
            digitisation_version=1,
            registration_version=1,
            verified_codes=[],
            findings=findings,
            query_items=[],
            rule_results=[],
            lookups=[],
            dropped=[],
            revisions=0,
            adverse=self.adverse,
        )


class FakeRiskService:
    """No red flags, or a referral when refer=True."""

    def __init__(self, *, refer: bool = False) -> None:
        self.refer = refer

    async def score(self, claim_id: UUID) -> RiskReport:
        hits = [RiskHit(rule_id="RF3", weight=60, detail="file reused")] if self.refer else []
        return RiskReport(
            claim_id=claim_id,
            registration_version=1,
            hits=hits,
            score=60 if self.refer else 0,
            refer=self.refer,
            explanation="explained" if self.refer else "No red flags.",
        )


class FakeAdjudicationService:
    """Drafts the given decision with a passing guardrail."""

    def __init__(self, decision: Decision = "approve") -> None:
        self.decision = decision

    async def draft(self, claim_id: UUID) -> DraftDecision:
        zero = Decimal("0.00")
        return DraftDecision(
            claim_id=claim_id,
            input_versions={},
            decision=self.decision,
            expected_decision=self.decision,
            amount=zero,
            payable_on_sheet=zero,
            available_balance=zero,
            reasons=[],
            query_items=[],
            summary="",
            self_check_errors=[],
            guardrail_errors=[],
            guardrail_passed=True,
            revisions=0,
        )
