"""Fakes of the stage contracts for workflow tests: clean results, no checkpoints."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.adjudication.contract import Decision, DraftDecision
from app.services.bill_review.contract import DeductionSheet, LineEdit
from app.services.classification.contract import DocumentSet
from app.services.communication.contract import Letter
from app.services.decision.contract import (
    ApproverOutcome,
    CrcOutcome,
    CrcReview,
    DecisionRecord,
    InvalidDecision,
    Routing,
)
from app.services.digitisation.contract import ClaimRecord
from app.services.intake.contract import ClaimForm, Submission
from app.services.medical_review.contract import (
    Citation,
    Finding,
    InvalidOverrule,
    MedicalCoding,
    MedicalFindings,
)
from app.services.query.contract import Query, QueryItem
from app.services.registration.contract import (
    MatchedHospital,
    MatchedMember,
    RegistrationOverrides,
    RegistrationResult,
)
from app.services.risk.contract import InvalidVerdict, RiskHit, RiskReport, Verdict
from app.services.settlement.contract import PaymentNotReleased, SettlementRecord
from app.shared.types import DocType, RequestType, Role

_NAMESPACE = uuid.UUID("6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0001")


def claim_id_for(submission_id: UUID) -> UUID:
    return uuid.uuid5(_NAMESPACE, str(submission_id))


class FakeIntakeService:
    """Every submission ID is a known submission whose claim ID derives from it."""

    async def accept(self, submission: Any) -> Submission:
        raise NotImplementedError("workflow tests start claims directly")

    async def start_missing_workflows(self) -> list[UUID]:
        return []

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
        self.overrules: list[tuple[list[int], str, str]] = []
        self.calls = 0
        self.coded: list[int] = []  # the call number of each code() and review()
        self.reviewed: list[int] = []

    async def code(self, claim_id: UUID) -> MedicalCoding:
        self.calls += 1
        self.coded.append(self.calls)
        return MedicalCoding(
            claim_id=claim_id,
            digitisation_version=1,
            registration_version=1,
            verified_codes=[],
            lookups=[],
            dropped=[],
            revisions=0,
        )

    async def overrule(
        self, claim_id: UUID, finding_ids: list[int], reason: str, *, actor: str
    ) -> MedicalFindings:
        findings = await self.review(claim_id)
        known = {f.finding_id for f in findings.findings}
        if not finding_ids or not set(finding_ids) <= known or not reason.strip():
            raise InvalidOverrule(f"cannot overrule {finding_ids}; findings are {known}")
        self.overrules.append((finding_ids, reason.strip(), actor))
        return findings.model_copy(update={"adverse": False})

    async def review(self, claim_id: UUID) -> MedicalFindings:
        self.calls += 1
        self.reviewed.append(self.calls)
        findings = (
            [
                Finding(
                    finding_id=1,
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
        self.verdicts: list[tuple[Verdict, str, str]] = []

    async def record_verdict(
        self, claim_id: UUID, verdict: Verdict, note: str, *, actor: str
    ) -> RiskReport:
        if not note.strip():
            raise InvalidVerdict("a verdict needs a note")
        self.verdicts.append((verdict, note.strip(), actor))
        report = await self.score(claim_id)
        return report.model_copy(update={"verdict": verdict, "verdict_note": note.strip()})

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


class FakeDecisionService:
    """Routes every claim to the approver band and records outcomes as given."""

    def __init__(self, draft: Decision = "approve") -> None:
        self.draft = draft
        self.records: list[DecisionRecord] = []

    async def route(self, claim_id: UUID) -> Routing:
        return Routing(
            claim_id=claim_id,
            adjudication_version=1,
            draft_decision=self.draft,
            draft_guardrail_passed=True,
            amount=Decimal("0.00"),
            band="approver",
            role=Role.APPROVER,
            authority_amount=Decimal("0.00"),
            crc_required=self.draft in ("partial", "reject"),
        )

    async def record(self, claim_id: UUID, outcome: ApproverOutcome) -> DecisionRecord:
        if outcome.action == "approve" and self.draft == "query":
            raise InvalidDecision("a query cannot be approved as it stands")
        final: Any = None
        if outcome.action == "approve":
            final = self.draft
        elif outcome.action == "override":
            final = outcome.decision
        record = DecisionRecord(
            claim_id=claim_id,
            routing=await self.route(claim_id),
            action=outcome.action,
            approver_id=outcome.approver_id,
            task_id=outcome.task_id,
            final_decision=final,
            final_amount=Decimal("0.00"),
            overrides=[],
            reason=outcome.reason,
            send_back_stage=outcome.stage if outcome.action == "send_back" else None,
            send_back_note=outcome.note,
            crc_required=final in ("partial", "reject"),
            decided_at=datetime(2026, 11, 19, tzinfo=UTC),
        )
        self.records.append(record)
        return record

    async def review_crc(self, claim_id: UUID, outcome: CrcOutcome) -> DecisionRecord:
        latest = self.records[-1]
        record = latest.model_copy(
            update={
                "crc": CrcReview(
                    reviewer_id=outcome.reviewer_id,
                    task_id=outcome.task_id,
                    action=outcome.action,
                    send_back_stage=outcome.stage if outcome.action == "send_back" else None,
                    note=outcome.note,
                    reviewed_at=datetime(2026, 11, 19, tzinfo=UTC),
                )
            }
        )
        self.records.append(record)
        return record

    async def latest(self, claim_id: UUID) -> DecisionRecord | None:
        return self.records[-1] if self.records else None


class FakeCommunicationService:
    def __init__(self) -> None:
        self.issued: list[tuple[UUID, UUID | None]] = []  # (claim, query) per letter

    def _letter(self, claim_id: UUID, kind: Any, query_id: UUID | None) -> Letter:
        self.issued.append((claim_id, query_id))
        return Letter(
            letter_id=uuid.uuid4(),
            claim_id=claim_id,
            claim_number="CLM-2026-000001",
            kind=kind,
            recipient=None,
            addressee="A",
            decision_version=None if query_id else 1,
            query_id=query_id,
            amount=Decimal("0.00"),
            claimed=None,
            deductions=[],
            raw_reasons=[],
            reasons=[],
            plain_language=False,
            query_items=[],
            reply_by=None,
            pdf_key=uuid.uuid4().hex,
            sent=False,
            created_at=datetime(2026, 11, 19, tzinfo=UTC),
        )

    async def issue_letter(self, claim_id: UUID) -> Letter:
        return self._letter(claim_id, "approval", None)

    async def issue_query_letter(self, claim_id: UUID, query_id: UUID) -> Letter:
        return self._letter(claim_id, "query", query_id)


class FakeQueryService:
    """Always one missing document to ask for. Records reminders, replies and deadlines."""

    def __init__(self, reply_days: int = 15, reminder_days: list[int] | None = None) -> None:
        self.reply_days = reply_days
        self.reminder_days = [3, 7] if reminder_days is None else reminder_days
        self.queries: dict[UUID, Query] = {}
        self.calls: list[tuple[str, Any]] = []

    async def outstanding_items(self, claim_id: UUID) -> list[QueryItem]:
        return [QueryItem(text="Payment receipt", source="missing_document")]

    async def raise_query(self, claim_id: UUID, items: list[QueryItem]) -> Query:
        now = datetime(2026, 11, 19, tzinfo=UTC)
        query = Query(
            query_id=uuid.uuid4(),
            claim_id=claim_id,
            items=items,
            status="open",
            letter_id=None,
            raised_at=now,
            deadline=now,
            reply_window_days=self.reply_days,
            reminder_days=self.reminder_days,
            reminders=[],
            reply_submission_id=None,
            answered_at=None,
        )
        self.queries[query.query_id] = query
        self.calls.append(("raise", query.query_id))
        return query

    async def attach_reply(self, query_id: UUID, submission_id: UUID) -> Query:
        self.calls.append(("reply", submission_id))
        return self.queries[query_id].model_copy(
            update={"status": "answered", "reply_submission_id": submission_id}
        )

    async def record_reminder(self, query_id: UUID, day: int) -> Query:
        self.calls.append(("reminder", day))
        return self.queries[query_id]

    async def mark_overdue(self, query_id: UUID) -> Query:
        self.calls.append(("overdue", query_id))
        return self.queries[query_id]

    async def get(self, query_id: UUID) -> Query | None:
        return self.queries.get(query_id)

    async def open_query(self, claim_id: UUID) -> Query | None:
        return None


class FakeSettlementService:
    """Records the order of calls. A paid claim is refused without a release."""

    def __init__(self, final: Decision = "approve") -> None:
        self.final = final
        self.calls: list[tuple[str, UUID | None]] = []
        # What the decision pays now: zero for a reject, or a reconsideration that adds
        # nothing; set it to zero to test that case.
        self.to_pay = Decimal("1000.00")

    @property
    def paid(self) -> Decimal:
        return self.to_pay if self.final in ("approve", "partial") else Decimal("0.00")

    def _record(self, claim_id: UUID, status: Any) -> SettlementRecord:
        return SettlementRecord(
            claim_id=claim_id,
            decision_version=1,
            final_decision=self.final,  # type: ignore[arg-type]
            paid_amount=self.paid,
            payee=None,
            payee_checks=[],
            status=status,
            released_by=None,
            payment_ref=None,
            block_id=None,
            utilised=Decimal("0.00"),
            released=Decimal("0.00"),
            closed_at=None,
        )

    async def check_payee(self, claim_id: UUID) -> SettlementRecord:
        self.calls.append(("check_payee", None))
        return self._record(claim_id, "awaiting_release")

    async def settle(self, claim_id: UUID, *, released_by: UUID | None) -> SettlementRecord:
        if self.paid > 0 and released_by is None:
            raise PaymentNotReleased("needs a finance release")
        self.calls.append(("settle", released_by))
        return self._record(claim_id, "settled")


class FakeCashlessService:
    """Implements the CashlessService contract. Each follow-up's request type comes from
    requests (submission ID -> type); any other submission is the pre-auth. The clock
    starts when the request begins, in workflow time: 1 hour, or 3 for a discharge."""

    def __init__(self) -> None:
        self.requests: dict[UUID, RequestType] = {}
        self.calls: list[tuple[str, str]] = []
        self.current = RequestType.PREAUTH
        self.closed = False
        self.clocks: list[datetime | None] = []

    async def begin_request(self, claim_id: UUID, submission_id: UUID) -> Any:
        from temporalio import activity

        from app.services.cashless.contract import CashlessRequest

        self.current = self.requests.get(submission_id, RequestType.PREAUTH)
        self.calls.append(("begin_request", self.current.value))
        started = activity.info().current_attempt_scheduled_time
        hours = {RequestType.DISCHARGE: 3, RequestType.CANCELLATION: None}.get(self.current, 1)
        self.clocks.append(started + timedelta(hours=hours) if hours else None)
        return CashlessRequest(
            claim_id=claim_id,
            submission_id=submission_id,
            request_type=self.current,
            received_at=started,
            clock_deadline=started + timedelta(hours=hours) if hours else None,
        )

    def _authorisation(self, claim_id: UUID, status: Any, request: RequestType) -> Any:
        from app.services.cashless.contract import Authorisation

        zero = Decimal("0.00")
        return Authorisation(
            claim_id=claim_id,
            request_type=request,
            submission_id=None,
            decision_version=1,
            final_decision="approve",
            approved_now=zero,
            previous_total=zero,
            approved_total=zero,
            approved_admissible=zero,
            diagnosis_codes=[],
            block_id=None,
            blocked=zero,
            status=status,
            authorised_at=datetime(2026, 11, 19, tzinfo=UTC),
        )

    async def authorise(self, claim_id: UUID) -> Any:
        self.calls.append(("authorise", self.current.value))
        status = "final" if self.current is RequestType.DISCHARGE else "awaiting_discharge"
        return self._authorisation(claim_id, status, self.current)

    async def latest_authorisation(self, claim_id: UUID) -> Any:
        return None

    async def mark_awaiting_discharge(self, claim_id: UUID) -> None:
        self.calls.append(("awaiting_discharge", self.current.value))

    async def cancel(self, claim_id: UUID, submission_id: UUID) -> Any:
        self.calls.append(("cancel", RequestType.CANCELLATION.value))
        self.closed = True
        return self._authorisation(claim_id, "cancelled", RequestType.CANCELLATION)

    async def mark_originals_received(self, claim_id: UUID, user_id: UUID) -> Any:
        raise NotImplementedError
