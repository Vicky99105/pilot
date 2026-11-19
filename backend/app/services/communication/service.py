"""Default communication: builds each letter from the decision record and the deduction
sheet, renders it with reportlab, stores it in the FileStore and sends it."""

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    ClaimRow,
    LetterRow,
    QueryRow,
    ReconsiderationRow,
    SubmissionRow,
    UserRow,
    utcnow,
)
from app.database.repositories.stage_records import StageRecordRepository, append_in_session
from app.services.adjudication.contract import DraftDecision
from app.services.bill_review.contract import DeductionSheet
from app.services.communication._helpers import (
    PLAIN_PURPOSE,
    PlainReasons,
    faithful,
    plain_messages,
    render_pdf,
    row_reason,
)
from app.services.communication.contract import (
    DeductionRow,
    Letter,
    LetterKind,
    NotReadyForLetter,
    ReconsiderationNote,
)
from app.services.decision.contract import DecisionRecord
from app.services.file_store.contract import FileStore
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.notification.contract import (
    Attachment,
    Notification,
    NotificationService,
)
from app.shared.errors import PermanentError
from app.shared.types import RequestType, Stage

__all__ = ["PLAIN_PURPOSE", "DefaultCommunicationService"]

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")
KINDS: dict[str, LetterKind] = {"approve": "approval", "partial": "partial", "reject": "rejection"}


class DefaultCommunicationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        file_store: FileStore,
        gateway: ModelGateway,
        notifications: NotificationService,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._files = file_store
        self._gateway = gateway
        self._notifications = notifications
        self._records = StageRecordRepository(sessions)
        self._now = now

    async def issue_letter(self, claim_id: UUID) -> Letter:
        stored = await self._records.latest(claim_id, Stage.DECISION)
        if stored is None:
            raise NotReadyForLetter(f"claim {claim_id} has no decision")
        record = DecisionRecord.model_validate(stored.payload)
        if record.final_decision is None:
            raise NotReadyForLetter(f"claim {claim_id} was sent back; no final decision")
        if record.crc_required and (record.crc is None or record.crc.action != "uphold"):
            raise NotReadyForLetter(f"the {record.final_decision} awaits the CRC's approval")
        existing = await self._existing(
            LetterRow.claim_id == claim_id,
            LetterRow.decision_version == stored.version,
            LetterRow.query_id.is_(None),
        )
        if existing is not None:
            return await self._deliver(existing)

        sheet = await self._sheet(claim_id)
        kind = KINDS[record.final_decision]
        rows: list[DeductionRow] = []
        raw: list[str] = []
        clauses: list[list[str]] = []
        if kind == "rejection":
            draft = await self._draft(claim_id)
            for reason in draft.reasons:
                ref = f" (clause {reason.clause_ref})" if reason.clause_ref else ""
                raw.append(f"{reason.text.rstrip('.')}.{ref}")
                clauses.append([reason.clause_ref] if reason.clause_ref else [])
            if record.overrides and record.reason:
                raw.append(record.reason)
                clauses.append([])
        else:
            rows = _deduction_rows(sheet, record)
            raw = [row_reason(r) for r in rows]
            clauses = [[r.clause_ref] if r.clause_ref else [] for r in rows]
        reasons, plain = await self._plain(claim_id, raw, clauses)
        extra: dict[str, Any] = {"amount": record.final_amount}
        authorised = await self._authorisation(claim_id) if kind != "rejection" else None
        if authorised is not None:
            kind = "authorisation"
            extra = {
                "amount": Decimal(str(authorised["approved_now"])),
                "request_type": authorised["request_type"],
                "previous_total": Decimal(str(authorised["previous_total"])),
                "approved_total": Decimal(str(authorised["approved_total"])),
            }
            if authorised["request_type"] == RequestType.DISCHARGE.value:
                extra["amount"] = record.final_amount
        note = await self._reconsideration(claim_id, record)
        if note is not None:
            extra["reconsideration"] = note
        letter = await self._letter(
            claim_id,
            kind=kind,
            decision_version=stored.version,
            claimed=sheet.claimed_total,
            deductions=rows,
            raw_reasons=raw,
            reasons=reasons,
            plain_language=plain,
            **extra,
        )
        return await self._store_and_send(letter)

    async def _reconsideration(
        self, claim_id: UUID, record: DecisionRecord
    ) -> ReconsiderationNote | None:
        """The reconsideration the decision answers: the claim's latest one asked before the
        decision was made. None when the decision answers none."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(ReconsiderationRow)
                .where(
                    ReconsiderationRow.claim_id == claim_id,
                    ReconsiderationRow.requested_at <= record.decided_at,
                )
                .order_by(ReconsiderationRow.number.desc())
                .limit(1)
            )
        if row is None:
            return None
        payable = record.final_amount if record.final_decision in ("approve", "partial") else ZERO
        return ReconsiderationNote(
            number=row.number,
            grounds=row.grounds,
            previous_decision=row.previous_decision,
            previously_paid=row.previously_paid,
            paid_now=max(payable - row.previously_paid, ZERO),
        )

    async def _authorisation(self, claim_id: UUID) -> dict[str, Any] | None:
        """The authorisation of the claim's current cashless request, if it has one."""
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
        if claim is None or claim.request_type != RequestType.PREAUTH.value:
            return None
        stored = await self._records.latest(claim_id, Stage.AUTHORISATION)
        if stored is None or stored.payload.get("request_type") != claim.current_request:
            return None
        return stored.payload

    async def issue_query_letter(self, claim_id: UUID, query_id: UUID) -> Letter:
        existing = await self._existing(LetterRow.query_id == query_id)
        if existing is not None:
            return await self._deliver(existing)
        async with self._sessions() as session:
            query = await session.get(QueryRow, query_id)
        if query is None or query.claim_id != claim_id:
            raise NotReadyForLetter(f"query {query_id} is not on claim {claim_id}")
        letter = await self._letter(
            claim_id,
            kind="query",
            query_id=query_id,
            query_items=[item["text"] for item in query.items],
            reply_by=query.deadline,
        )
        return await self._store_and_send(letter)

    async def _letter(self, claim_id: UUID, *, kind: LetterKind, **fields: Any) -> Letter:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
            assert claim is not None
            first = await session.get(SubmissionRow, claim.submission_id)
            user = (
                await session.get(UserRow, first.submitted_by)
                if first and first.submitted_by
                else None
            )
        defaults: dict[str, Any] = {
            "decision_version": None,
            "query_id": None,
            "amount": ZERO,
            "claimed": None,
            "deductions": [],
            "raw_reasons": [],
            "reasons": [],
            "plain_language": False,
            "query_items": [],
            "reply_by": None,
        }
        return Letter(
            letter_id=uuid4(),
            claim_id=claim_id,
            claim_number=claim.claim_number,
            kind=kind,
            recipient=user.email if user else None,
            addressee=str((first.form or {}).get("patient_name") or "the claimant")
            if first
            else "the claimant",
            pdf_key="",
            sent=False,
            created_at=self._now(),
            **(defaults | fields),
        )

    async def _plain(
        self, claim_id: UUID, raw: list[str], clauses: list[list[str]]
    ) -> tuple[list[str], bool]:
        """One FAST_TEXT call. The rewrite is used only when it keeps every number and clause."""
        if not raw:
            return raw, False
        try:
            reply = await self._gateway.structured(
                ModelRole.FAST_TEXT,
                plain_messages(raw),
                PlainReasons,
                claim_id=claim_id,
                purpose=PLAIN_PURPOSE,
            )
        except PermanentError:
            logger.warning("Plain-language rewrite failed for claim %s", claim_id, exc_info=True)
            return raw, False
        items = [i.strip() for i in reply.items]
        if faithful(raw, items, clauses):
            return items, True
        return raw, False

    async def _store_and_send(self, letter: Letter) -> Letter:
        pdf = render_pdf(letter)
        letter.pdf_key = await self._files.put(pdf, content_type="application/pdf")
        async with self._sessions.begin() as session:
            session.add(
                LetterRow(
                    id=letter.letter_id,
                    claim_id=letter.claim_id,
                    kind=letter.kind,
                    query_id=letter.query_id,
                    decision_version=letter.decision_version,
                    recipient=letter.recipient,
                    pdf_key=letter.pdf_key,
                    sent=False,
                    content=letter.model_dump(mode="json"),
                    created_at=letter.created_at,
                )
            )
            await append_in_session(
                session,
                letter.claim_id,
                Stage.COMMUNICATION,
                letter.model_dump(mode="json"),
                created_by="code",
            )
        return await self._deliver(letter)

    async def _deliver(self, letter: Letter) -> Letter:
        """Send a stored letter that has not been sent or refused yet, and record the result."""
        if letter.sent or letter.send_error:
            return letter
        if letter.recipient is None:
            return await self._mark(letter, sent=False, error="no email address on file")
        pdf = await self._files.get(letter.pdf_key)
        try:
            receipt = await self._notifications.send(
                Notification(
                    to=letter.recipient,
                    subject=f"Claim {letter.claim_number or ''}: {letter.kind} letter".strip(),
                    body=f"Dear {letter.addressee},\n\nPlease find your claim letter attached.",
                    attachments=[
                        Attachment(
                            filename=f"{letter.kind}-letter.pdf",
                            content_type="application/pdf",
                            data=pdf,
                        )
                    ],
                )
            )
        except PermanentError as exc:
            return await self._mark(letter, sent=False, error=f"{type(exc).__name__}: {exc}")
        if not receipt.delivered:
            return letter  # recorded only (shadow mode): not sent, and nothing went wrong
        return await self._mark(letter, sent=True, error=None)

    async def _mark(self, letter: Letter, *, sent: bool, error: str | None) -> Letter:
        updated = letter.model_copy(update={"sent": sent, "send_error": error})
        async with self._sessions.begin() as session:
            row = await session.get(LetterRow, letter.letter_id)
            assert row is not None
            row.sent, row.send_error = sent, error
            row.content = updated.model_dump(mode="json")
        return updated

    async def _existing(self, *where: Any) -> Letter | None:
        async with self._sessions() as session:
            row = await session.scalar(select(LetterRow).where(*where).limit(1))
        return Letter.model_validate(row.content) if row else None

    async def _sheet(self, claim_id: UUID) -> DeductionSheet:
        stored = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if stored is None:
            raise NotReadyForLetter(f"claim {claim_id} has no deduction sheet")
        return DeductionSheet.model_validate(stored.payload)

    async def _draft(self, claim_id: UUID) -> DraftDecision:
        stored = await self._records.latest(claim_id, Stage.ADJUDICATION)
        if stored is None:
            raise NotReadyForLetter(f"claim {claim_id} has no adjudication draft")
        return DraftDecision.model_validate(stored.payload)


def _deduction_rows(sheet: DeductionSheet, record: DecisionRecord) -> list[DeductionRow]:
    rows = [
        DeductionRow(
            line_id=ln.line_id,
            description=ln.description,
            claimed=ln.claimed,
            deducted=ln.deducted,
            reason=ln.reason or "deducted under the policy",
            clause_ref=ln.clause_ref,
        )
        for ln in sheet.lines
        if ln.deducted > 0
    ]
    if sheet.co_pay > 0:
        co_pay = next((r for r in sheet.rule_results if r.rule_id == "R21"), None)
        rows.append(
            DeductionRow(
                line_id=None,
                description="Co-payment",
                claimed=sheet.admissible_total,
                deducted=sheet.co_pay,
                reason=co_pay.reason if co_pay else "co-payment borne by the insured person",
                clause_ref=co_pay.clause_ref if co_pay else None,
            )
        )
    adjustment = sheet.payable - record.final_amount
    if adjustment > 0:
        rows.append(
            DeductionRow(
                line_id=None,
                description="Approver adjustment",
                claimed=sheet.payable,
                deducted=adjustment,
                reason=record.reason or "adjusted by the approver",
                clause_ref=None,
            )
        )
    return rows
