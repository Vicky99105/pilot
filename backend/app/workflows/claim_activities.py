"""ClaimWorkflow activities. Thin wrappers that call service contracts only.

Every stage calls its service. Each activity records the claim's events through the
Monitoring contract, which also keeps claims.status at the current stage.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.services.adjudication.contract import AdjudicationService
from app.services.bill_review.contract import BillReviewService, LineEdit
from app.services.cashless.contract import CashlessRequest, CashlessService
from app.services.classification.contract import ClassificationService
from app.services.communication.contract import CommunicationService
from app.services.decision.contract import ApproverOutcome, CrcOutcome, DecisionService
from app.services.digitisation.contract import DigitisationService
from app.services.intake.contract import IntakeService
from app.services.medical_review.contract import MedicalReviewService
from app.services.model_gateway.contract import ModelUnavailable
from app.services.monitoring.contract import Monitoring
from app.services.query.contract import NothingToAsk, Query, QueryService
from app.services.registration.contract import RegistrationOverrides, RegistrationService
from app.services.risk.contract import RiskService
from app.services.settlement.contract import SettlementService
from app.services.tasks.contract import TaskService
from app.shared.errors import PermanentError
from app.shared.types import DocType, Stage
from app.workflows.claim_models import (
    CashlessStepInput,
    ClaimInput,
    ClockInput,
    CorrectionInput,
    DecisionStep,
    EventInput,
    OpenTaskInput,
    OutcomeInput,
    OverruleOutcome,
    QueryRaised,
    QueryStepInput,
    StageInput,
    StageResult,
    VerdictOutcome,
)

R = TypeVar("R")
_LINE_EDITS = TypeAdapter(list[LineEdit])
MODEL_UNAVAILABLE = "ModelUnavailable"  # the ApplicationError type of a retryable model failure


async def _non_retryable_on_permanent(call: Awaitable[R]) -> R:
    """Temporal matches non-retryable types by exact class name, so subclasses of
    PermanentError are re-raised as one non-retryable PermanentError."""
    try:
        return await call
    except PermanentError as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}", type="PermanentError", non_retryable=True
        ) from exc


class ClaimActivities:
    def __init__(
        self,
        monitoring: Monitoring,
        tasks: TaskService,
        *,
        intake: IntakeService,
        classification: ClassificationService,
        digitisation: DigitisationService,
        registration: RegistrationService,
        bill_review: BillReviewService,
        medical_review: MedicalReviewService,
        risk: RiskService,
        adjudication: AdjudicationService,
        decision: DecisionService,
        communication: CommunicationService,
        query: QueryService,
        settlement: SettlementService,
        cashless: CashlessService,
    ) -> None:
        self._monitoring = monitoring
        self._tasks = tasks
        self._intake = intake
        self._classification = classification
        self._digitisation = digitisation
        self._registration = registration
        self._bill_review = bill_review
        self._medical_review = medical_review
        self._risk = risk
        self._adjudication = adjudication
        self._decision = decision
        self._communication = communication
        self._query = query
        self._settlement = settlement
        self._cashless = cashless

    async def _run(self, claim_id: UUID, stage: Stage, call: Awaitable[R]) -> R:
        """Run a stage's service call. A permanent failure is recorded as an event, so it
        shows in the claim's history, and is not retried. A model that is unavailable is
        retried after the wait the provider asked for, when it named one."""
        try:
            return await call
        except ModelUnavailable as exc:
            delay = exc.retry_after_seconds
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}"[:2000],
                type=MODEL_UNAVAILABLE,
                next_retry_delay=timedelta(seconds=delay) if delay is not None else None,
            ) from exc
        except PermanentError as exc:
            await self._monitoring.record_event(
                claim_id=claim_id,
                stage=stage,
                action="stage_failed",
                actor="system",
                detail={"error": f"{type(exc).__name__}: {exc}"[:2000]},
            )
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}", type="PermanentError", non_retryable=True
            ) from exc

    async def _stage_done(
        self, inp: StageInput, detail: dict[str, Any] | None = None, **result: Any
    ) -> StageResult:
        if inp.submission_id:
            detail = {"submission_id": str(inp.submission_id), **(detail or {})}
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=inp.stage,
                action="stage_completed",
                actor="system",
                detail=detail,
            )
        )
        return StageResult(claim_id=inp.claim_id, stage=inp.stage, **result)

    @activity.defn(name="intake")
    async def intake(self, claim: ClaimInput) -> StageResult:
        submission = await _non_retryable_on_permanent(self._intake.get(claim.submission_id))
        if submission is None:
            raise ApplicationError(
                f"submission {claim.submission_id} not found",
                type="PermanentError",
                non_retryable=True,
            )
        return await self._stage_done(
            StageInput(
                claim_id=submission.claim_id,
                stage=Stage.INTAKE,
                submission_id=submission.submission_id,
            ),
            {
                "files": len(submission.files),
                "missing_documents": [d.value for d in submission.missing_documents],
            },
        )

    @activity.defn(name="classification")
    async def classification(self, inp: StageInput) -> StageResult:
        if inp.submission_id is None:
            raise ApplicationError(
                "classification needs a submission", type="PermanentError", non_retryable=True
            )
        doc_set = await self._run(
            inp.claim_id, inp.stage, self._classification.classify(inp.submission_id)
        )
        payload = {
            "submission_id": str(doc_set.submission_id),
            "pages": [
                {
                    "page_id": str(p.page_id),
                    "seq": p.seq,
                    "doc_type": p.doc_type.value,
                    "confidence": p.confidence,
                    "image_key": p.image_key,
                    "quality_warning": p.quality_warning,
                }
                for p in doc_set.pages
            ],
            "mismatches": [m.model_dump(mode="json") for m in doc_set.mismatches],
        }
        return await self._stage_done(
            inp,
            {
                "pages": len(doc_set.pages),
                "documents": [d.doc_type.value for d in doc_set.documents],
                "needs_review": doc_set.needs_review,
            },
            needs_checkpoint=doc_set.needs_review,
            checkpoint_payload=payload,
        )

    @activity.defn(name="digitisation")
    async def digitisation(self, inp: StageInput) -> StageResult:
        record = await self._run(inp.claim_id, inp.stage, self._digitisation.extract(inp.claim_id))
        payload: dict[str, Any] = {
            "low_confidence_fields": record.low_confidence_fields,
            "failed_checks": [c.model_dump(mode="json") for c in record.failed_checks],
        }
        paths = [
            *record.low_confidence_fields,
            *(p for c in record.failed_checks for p in c.fields),
        ]
        dumped = record.model_dump(mode="json")
        fields = {p: f for p in dict.fromkeys(paths) if (f := _at_path(dumped, p)) is not None}
        return await self._stage_done(
            inp,
            payload,
            needs_checkpoint=bool(record.low_confidence_fields or record.failed_checks),
            checkpoint_payload={**payload, "fields": fields},  # the flagged Fields, as they are
        )

    @activity.defn(name="apply_corrections")
    async def apply_corrections(self, inp: CorrectionInput) -> None:
        if inp.stage is Stage.CLASSIFICATION:
            labels = _labels(inp.outcome.get("labels") or {})
            call: Awaitable[Any] = self._classification.apply_labels(
                inp.claim_id, labels, actor=inp.actor
            )
            changed = sorted(str(p) for p in labels)
        elif inp.stage is Stage.DIGITISATION:
            fields = inp.outcome.get("fields") or {}
            call = self._digitisation.apply_corrections(inp.claim_id, fields, actor=inp.actor)
            changed = sorted(fields)
        elif inp.stage is Stage.REGISTRATION:
            overrides = _parse(RegistrationOverrides.model_validate, inp.outcome.get("overrides"))
            call = self._registration.apply_overrides(inp.claim_id, overrides, actor=inp.actor)
            changed = sorted(overrides.model_dump(exclude_defaults=True))
        elif inp.stage is Stage.BILL_REVIEW:
            edits = _parse(_LINE_EDITS.validate_python, inp.outcome.get("lines"))
            call = self._bill_review.apply_edits(inp.claim_id, edits, actor=inp.actor)
            changed = [f"line {e.line_id}" for e in edits]
        else:
            raise ApplicationError(
                f"{inp.stage} takes no corrections", type="PermanentError", non_retryable=True
            )
        await self._run(inp.claim_id, inp.stage, call)
        await self._monitoring.record_event(
            claim_id=inp.claim_id,
            stage=inp.stage,
            action="corrections_applied",
            actor=inp.actor,
            detail={"changed": changed},
        )

    @activity.defn(name="registration")
    async def registration(self, inp: StageInput) -> StageResult:
        result = await self._run(inp.claim_id, inp.stage, self._registration.register(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "claim_number": result.claim_number,
                "failed_rules": [r.rule_id for r in result.rule_results if not r.passed],
                "duplicates": [str(d.claim_id) for d in result.duplicates],
                "blocked": str(result.block_ref.amount) if result.block_ref else None,
            },
            needs_checkpoint=True,  # "confirm registration" is always on
            checkpoint_payload=result.model_dump(mode="json"),
        )

    @activity.defn(name="medical_coding")
    async def medical_coding(self, inp: StageInput) -> StageResult:
        coding = await self._run(inp.claim_id, inp.stage, self._medical_review.code(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "verified_codes": [v.code.code for v in coding.verified_codes],
                "procedure_codes": [p.code.code for p in coding.procedure_codes],
                "dropped": len(coding.dropped),
            },
        )

    @activity.defn(name="bill_review")
    async def bill_review(self, inp: StageInput) -> StageResult:
        sheet = await self._run(inp.claim_id, inp.stage, self._bill_review.review(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "claimed": str(sheet.claimed_total),
                "admissible": str(sheet.admissible_total),
                "payable": str(sheet.payable),
                "flagged_lines": sheet.flagged_lines,
                "packages": [p.package_code for p in sheet.package.packages]
                if sheet.package
                else [],
                "findings": len(sheet.findings),
            },
            needs_checkpoint=True,  # "confirm deductions" is always on
            checkpoint_payload=sheet.model_dump(mode="json"),
        )

    @activity.defn(name="medical_review")
    async def medical_review(self, inp: StageInput) -> StageResult:
        findings = await self._run(
            inp.claim_id, inp.stage, self._medical_review.review(inp.claim_id)
        )
        return await self._stage_done(
            inp,
            {
                "verified_codes": [v.code.code for v in findings.verified_codes],
                "procedure_codes": [p.code.code for p in findings.procedure_codes],
                "adverse": findings.adverse,
                "findings": len(findings.findings),
                "dropped": len(findings.dropped),
                "query_items": [q.doc_type.value for q in findings.query_items],
            },
            needs_checkpoint=findings.adverse,  # "medical findings" only when adverse
            checkpoint_payload=findings.model_dump(mode="json"),
        )

    @activity.defn(name="medical_outcome")
    async def medical_outcome(self, inp: OutcomeInput) -> None:
        """The medical officer's outcome: confirm the findings, or overrule some of them."""
        if inp.outcome.get("action") == "confirm":
            return
        outcome = _parse(OverruleOutcome.model_validate, inp.outcome)
        actor = f"user:{inp.outcome.get('completed_by')}"
        await _non_retryable_on_permanent(
            self._medical_review.overrule(
                inp.claim_id, outcome.finding_ids, outcome.reason, actor=actor
            )
        )
        await self._event(
            _at(inp),
            "findings_overruled",
            {"finding_ids": outcome.finding_ids, "reason": outcome.reason.strip()},
            actor=actor,
        )

    @activity.defn(name="investigation_outcome")
    async def investigation_outcome(self, inp: OutcomeInput) -> None:
        """The investigator's verdict, "cleared" or "fraud_confirmed", with a note."""
        outcome = _parse(VerdictOutcome.model_validate, inp.outcome)
        actor = f"user:{inp.outcome.get('completed_by')}"
        report = await _non_retryable_on_permanent(
            self._risk.record_verdict(inp.claim_id, outcome.action, outcome.note, actor=actor)
        )
        await self._event(
            _at(inp),
            "investigation_verdict",
            {"verdict": report.verdict, "note": report.verdict_note},
            actor=actor,
        )

    @activity.defn(name="risk")
    async def risk(self, inp: StageInput) -> StageResult:
        report = await self._run(inp.claim_id, inp.stage, self._risk.score(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "score": report.score,
                "refer": report.refer,
                "hits": [h.rule_id for h in report.hits],
            },
            needs_checkpoint=report.refer,  # "investigate" only when referred
            checkpoint_payload=report.model_dump(mode="json"),
        )

    @activity.defn(name="adjudication")
    async def adjudication(self, inp: StageInput) -> StageResult:
        draft = await self._run(inp.claim_id, inp.stage, self._adjudication.draft(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "decision": draft.decision,
                "amount": str(draft.amount),
                "guardrail_passed": draft.guardrail_passed,
                "guardrail_errors": draft.guardrail_errors,
                "self_check_errors": draft.self_check_errors,
            },
            draft=draft.decision,
            checkpoint_payload=draft.model_dump(mode="json"),
        )

    @activity.defn(name="decision")
    async def decision(self, inp: StageInput) -> StageResult:
        """Route the draft to an approver band. The stage completes when the decision is
        final: recorded by the approver and, for a partial or reject, upheld by the CRC."""
        routing = await self._run(inp.claim_id, inp.stage, self._decision.route(inp.claim_id))
        draft = await self._run(inp.claim_id, inp.stage, self._adjudication.draft(inp.claim_id))
        await self._event(
            inp,
            "decision_routed",
            {"band": routing.band, "amount": str(routing.amount), "draft": routing.draft_decision},
        )
        return StageResult(
            claim_id=inp.claim_id,
            stage=inp.stage,
            needs_checkpoint=True,  # "approve" is always on
            checkpoint_payload={
                "routing": routing.model_dump(mode="json"),
                "draft": draft.model_dump(mode="json"),
            },
            draft=routing.draft_decision,
            authority_amount=routing.authority_amount,
        )

    @activity.defn(name="record_decision")
    async def record_decision(self, inp: OutcomeInput) -> DecisionStep:
        outcome = _parse(
            ApproverOutcome.model_validate,
            {**inp.outcome, "approver_id": inp.outcome.get("completed_by"), "task_id": inp.task_id},
        )
        # A refused outcome is not a stage failure: the workflow reopens the checkpoint.
        record = await _non_retryable_on_permanent(self._decision.record(inp.claim_id, outcome))
        detail = {
            "action": record.action,
            "final_decision": record.final_decision,
            "amount": str(record.final_amount),
            "approver": str(record.approver_id),
            "overrides": [o.model_dump() for o in record.overrides],
        }
        await self._event(_at(inp), "decision_recorded", detail, actor=f"user:{record.approver_id}")
        if record.final_decision is not None and not record.crc_required:
            await self._event(
                _at(inp), "stage_completed", {"final_decision": record.final_decision}
            )
        return DecisionStep(
            final_decision=record.final_decision,
            crc_required=record.crc_required,
            send_back_stage=record.send_back_stage,
        )

    @activity.defn(name="review_crc")
    async def review_crc(self, inp: OutcomeInput) -> DecisionStep:
        outcome = _parse(
            CrcOutcome.model_validate,
            {**inp.outcome, "reviewer_id": inp.outcome.get("completed_by"), "task_id": inp.task_id},
        )
        record = await _non_retryable_on_permanent(self._decision.review_crc(inp.claim_id, outcome))
        assert record.crc is not None
        await self._event(
            _at(inp),
            "crc_reviewed",
            {"action": record.crc.action, "reviewer": str(record.crc.reviewer_id)},
            actor=f"user:{record.crc.reviewer_id}",
        )
        upheld = record.crc.action == "uphold"
        if upheld:
            await self._event(
                _at(inp), "stage_completed", {"final_decision": record.final_decision}
            )
        return DecisionStep(
            final_decision=record.final_decision if upheld else None,
            crc_required=True,
            send_back_stage=record.crc.send_back_stage,
        )

    @activity.defn(name="communication")
    async def communication(self, inp: StageInput) -> StageResult:
        letter = await self._run(
            inp.claim_id, inp.stage, self._communication.issue_letter(inp.claim_id)
        )
        return await self._stage_done(
            inp,
            {
                "letter_id": str(letter.letter_id),
                "kind": letter.kind,
                "amount": str(letter.amount),
                "plain_language": letter.plain_language,
                "sent": letter.sent,
                "send_error": letter.send_error,
            },
        )

    @activity.defn(name="begin_request")
    async def begin_request(self, inp: CashlessStepInput) -> CashlessRequest:
        """Make the follow-up the claim's current cashless request, with its clock."""
        request = await self._run(
            inp.claim_id,
            Stage.INTAKE,
            self._cashless.begin_request(inp.claim_id, inp.submission_id),
        )
        await self._event(
            StageInput(claim_id=inp.claim_id, stage=Stage.INTAKE),
            "request_started",
            {
                "request_type": request.request_type.value,
                "submission_id": str(request.submission_id),
                "clock_deadline": request.clock_deadline.isoformat()
                if request.clock_deadline
                else None,
            },
        )
        return request

    @activity.defn(name="authorisation")
    async def authorisation(self, inp: StageInput) -> StageResult:
        """After the decision of a cashless request: the approved total and its block."""
        authorised = await self._run(
            inp.claim_id, inp.stage, self._cashless.authorise(inp.claim_id)
        )
        await self._stage_done(
            inp,
            {
                "request_type": authorised.request_type.value,
                "approved_now": str(authorised.approved_now),
                "approved_total": str(authorised.approved_total),
                "blocked": str(authorised.blocked),
                "status": authorised.status,
            },
        )
        return StageResult(
            claim_id=inp.claim_id, stage=inp.stage, closed=authorised.status == "rejected"
        )

    @activity.defn(name="await_discharge")
    async def await_discharge(self, inp: StageInput) -> None:
        """The event first: Monitoring keeps the status at the event's stage, and the
        status the claim waits in is awaiting_discharge."""
        await self._event(inp, "awaiting_discharge")
        await self._run(
            inp.claim_id, inp.stage, self._cashless.mark_awaiting_discharge(inp.claim_id)
        )

    @activity.defn(name="cancel_request")
    async def cancel_request(self, inp: CashlessStepInput) -> None:
        cancelled = await self._run(
            inp.claim_id, Stage.SETTLEMENT, self._cashless.cancel(inp.claim_id, inp.submission_id)
        )
        at = StageInput(claim_id=inp.claim_id, stage=Stage.SETTLEMENT)
        await self._event(
            at,
            "request_cancelled",
            {"released_block": cancelled.block_id, "submission_id": str(inp.submission_id)},
        )
        await self._event(at, "claim_closed", {"closed_at": str(cancelled.authorised_at)})

    @activity.defn(name="query")
    async def query(self, inp: StageInput) -> QueryRaised:
        items = await self._run(
            inp.claim_id, inp.stage, self._query.outstanding_items(inp.claim_id)
        )

        async def raise_query() -> Query | None:
            try:
                return await self._query.raise_query(inp.claim_id, items)
            except NothingToAsk:
                return None  # everything was asked and answered: the approver decides

        query = await self._run(inp.claim_id, inp.stage, raise_query())
        if query is None:
            await self._event(inp, "query_not_raised", {"items": [i.text for i in items]})
            return QueryRaised(query_id=None)
        await self._stage_done(
            inp,
            {
                "query_id": str(query.query_id),
                "items": [i.text for i in query.items],
                "letter_id": str(query.letter_id) if query.letter_id else None,
                "deadline": query.deadline.isoformat(),
            },
        )
        return QueryRaised(
            query_id=query.query_id,
            reply_window_days=query.reply_window_days,
            reminder_days=query.reminder_days,
        )

    @activity.defn(name="query_reminder")
    async def query_reminder(self, inp: QueryStepInput) -> None:
        assert inp.day is not None
        await self._run(
            inp.claim_id, Stage.QUERY, self._query.record_reminder(inp.query_id, inp.day)
        )
        await self._event(
            _query(inp), "query_reminder", {"query_id": str(inp.query_id), "day": inp.day}
        )

    @activity.defn(name="query_overdue")
    async def query_overdue(self, inp: QueryStepInput) -> None:
        await self._run(inp.claim_id, Stage.QUERY, self._query.mark_overdue(inp.query_id))
        await self._event(_query(inp), "query_deadline_passed", {"query_id": str(inp.query_id)})

    @activity.defn(name="query_reply")
    async def query_reply(self, inp: QueryStepInput) -> None:
        assert inp.submission_id is not None
        query = await self._run(
            inp.claim_id,
            Stage.QUERY,
            self._query.attach_reply(inp.query_id, inp.submission_id),
        )
        await self._event(
            _query(inp),
            "query_answered",
            {"query_id": str(query.query_id), "submission_id": str(inp.submission_id)},
        )

    @activity.defn(name="settlement")
    async def settlement(self, inp: StageInput) -> StageResult:
        """Payee checks only. Payment waits for the finance release; see settle."""
        record = await self._run(
            inp.claim_id, inp.stage, self._settlement.check_payee(inp.claim_id)
        )
        await self._event(
            inp,
            "payee_checked",
            {
                "checks": [c.model_dump() for c in record.payee_checks],
                "paid": str(record.paid_amount),
            },
        )
        return StageResult(
            claim_id=inp.claim_id,
            stage=inp.stage,
            needs_checkpoint=record.paid_amount > 0,  # the finance release
            checkpoint_payload=record.model_dump(mode="json"),
        )

    @activity.defn(name="settle")
    async def settle(self, inp: OutcomeInput) -> None:
        """After the finance release (or, for a rejection, directly): pay, release the
        block and close the claim."""
        released_by = None
        if inp.outcome:
            if inp.outcome.get("action") != "release":
                raise ApplicationError(
                    'the finance outcome must be {"action": "release"} or a send-back',
                    type="PermanentError",
                    non_retryable=True,
                )
            released_by = _parse(UUID, inp.outcome.get("completed_by"))
        record = await _non_retryable_on_permanent(
            self._settlement.settle(inp.claim_id, released_by=released_by)
        )
        await self._event(
            _at(inp),
            "stage_completed",
            {
                "payment_id": record.payment_ref.payment_id if record.payment_ref else None,
                "paid": str(record.paid_amount),
                "utilised": str(record.utilised),
                "released": str(record.released),
            },
            actor=f"user:{released_by}" if released_by else "system",
        )
        await self._event(_at(inp), "claim_closed", {"closed_at": str(record.closed_at)})

    @activity.defn(name="monitoring")
    async def monitoring(self, inp: StageInput) -> StageResult:
        summary = await _non_retryable_on_permanent(self._monitoring.summarise(inp.claim_id))
        return await self._stage_done(
            inp,
            {
                "status": summary.status,
                "turnaround_seconds": summary.turnaround_seconds,
                "within_deadline": summary.within_deadline,
                "checkpoints": summary.checkpoints,
            },
        )

    async def _event(
        self,
        inp: StageInput,
        action: str,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "system",
    ) -> None:
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id, stage=inp.stage, action=action, actor=actor, detail=detail
            )
        )

    @activity.defn(name="reconcile_claims")
    async def reconcile_claims(self) -> list[str]:
        """Start ClaimWorkflow for claims saved while Temporal was down."""
        started = await _non_retryable_on_permanent(self._intake.start_missing_workflows())
        if started:
            activity.logger.info("Reconciler started workflows for %d claims", len(started))
        return [str(c) for c in started]

    @activity.defn(name="open_task")
    async def open_task(self, inp: OpenTaskInput) -> UUID:
        task = await _non_retryable_on_permanent(
            self._tasks.open_task(
                claim_id=inp.claim_id,
                workflow_id=inp.workflow_id,
                kind=inp.kind,
                role=inp.role,
                title=inp.title,
                payload=inp.payload,
                deadline=inp.deadline,
                authority_amount=inp.authority_amount,
            )
        )
        await self._monitoring.record_event(
            claim_id=inp.claim_id,
            stage=inp.stage,
            action="checkpoint_opened",
            actor="workflow",
            detail={"task_id": str(task.id), "kind": inp.kind, "role": inp.role.value},
        )
        return task.id

    @activity.defn(name="record_event")
    async def record_event(self, inp: EventInput) -> None:
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=inp.stage,
                action=inp.action,
                actor=inp.actor,
                detail=inp.detail,
                moves_status=inp.moves_status,
            )
        )

    @activity.defn(name="clock_resumed")
    async def clock_resumed(self, inp: ClockInput) -> None:
        """A resumed claim: its deadlines move on by the time it was held."""
        await _non_retryable_on_permanent(
            self._tasks.shift_deadlines(claim_id=inp.claim_id, seconds=inp.paused_seconds)
        )
        await _non_retryable_on_permanent(
            self._monitoring.record_event(
                claim_id=inp.claim_id,
                stage=Stage.MONITORING,
                action="clock_resumed",
                actor="workflow",
                detail={
                    "paused_seconds": inp.paused_seconds,
                    "clock_deadline": inp.clock_deadline.isoformat()
                    if inp.clock_deadline
                    else None,
                },
                moves_status=False,
            )
        )

    def all(self) -> list[Callable[..., Awaitable[Any]]]:
        names = [s.value for s in Stage] + [
            "open_task",
            "record_event",
            "apply_corrections",
            "record_decision",
            "review_crc",
            "medical_outcome",
            "investigation_outcome",
            "reconcile_claims",
            "query_reminder",
            "query_overdue",
            "query_reply",
            "settle",
            "begin_request",
            "await_discharge",
            "cancel_request",
            "clock_resumed",
        ]
        return [getattr(self, n) for n in names]


def _parse(parse: Callable[[Any], R], raw: Any) -> R:
    """A person's outcome that does not parse is a permanent error: the workflow reopens
    the checkpoint with the reason."""
    try:
        return parse(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ApplicationError(
            f"invalid outcome: {exc}", type="PermanentError", non_retryable=True
        ) from exc


def _labels(raw: dict[str, Any]) -> dict[UUID, DocType]:
    try:
        return {UUID(page_id): DocType(doc_type) for page_id, doc_type in raw.items()}
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            f"labels must map page IDs to document types: {exc}",
            type="PermanentError",
            non_retryable=True,
        ) from exc


def _at_path(data: Any, path: str) -> dict[str, Any] | None:
    """The dumped Field at a dotted path such as "bill.lines.0.amount", if there is one."""
    node = data
    for part in path.split("."):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node if isinstance(node, dict) and "value" in node else None


def _at(inp: OutcomeInput) -> StageInput:
    return StageInput(claim_id=inp.claim_id, stage=inp.stage)


def _query(inp: QueryStepInput) -> StageInput:
    return StageInput(claim_id=inp.claim_id, stage=Stage.QUERY)
