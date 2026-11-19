"""Default cashless service: the claim's current request, authorisations against the sum
insured block through the MoneyService contract, cancellation and the originals."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, SubmissionRow, utcnow
from app.database.repositories.stage_records import StageRecordRepository, append_in_session
from app.integrations.core.contract import MoneyService
from app.services.cashless.contract import (
    AWAITING_DISCHARGE,
    ORIGINALS_COURIER_DAYS,
    Authorisation,
    AuthorisationStatus,
    CashlessRequest,
    NoOriginalsDue,
    NotCashless,
    NotReadyToAuthorise,
    OriginalsStatus,
)
from app.shared.types import RequestType, Stage

ZERO = Decimal("0.00")
CLOSED = "closed"
RAISES_BLOCK = (RequestType.PREAUTH, RequestType.ENHANCEMENT)


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


class DefaultCashlessService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        money: MoneyService,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._money = money
        self._now = now
        self._records = StageRecordRepository(sessions)

    async def begin_request(self, claim_id: UUID, submission_id: UUID) -> CashlessRequest:
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            submission = await session.get(SubmissionRow, submission_id)
            if claim is None or claim.request_type != RequestType.PREAUTH.value:
                raise NotCashless(f"claim {claim_id} was not opened by a pre-auth")
            if submission is None or submission.claim_id != claim_id:
                raise NotCashless(f"submission {submission_id} is not on claim {claim_id}")
            claim.current_request = submission.request_type
            claim.current_submission_id = submission_id
            if submission.clock_deadline is not None:
                claim.deadline = submission.clock_deadline
            return CashlessRequest(
                claim_id=claim_id,
                submission_id=submission_id,
                request_type=RequestType(submission.request_type),
                received_at=submission.created_at,
                clock_deadline=submission.clock_deadline,
            )

    async def authorise(self, claim_id: UUID) -> Authorisation:
        stored = await self._records.latest(claim_id, Stage.DECISION)
        if stored is None:
            raise NotReadyToAuthorise(f"claim {claim_id} has no decision")
        decision = stored.payload
        final = decision.get("final_decision")
        if final is None:
            raise NotReadyToAuthorise(f"claim {claim_id} was sent back; no final decision")
        crc = decision.get("crc") or {}
        if decision.get("crc_required") and crc.get("action") != "uphold":
            raise NotReadyToAuthorise(f"the {final} awaits the CRC")
        previous = await self.latest_authorisation(claim_id)
        if previous is not None and previous.decision_version == stored.version:
            return previous

        claim = await self._claim(claim_id)
        request = RequestType(claim.current_request or claim.request_type)
        before = previous.approved_total if previous else ZERO
        before_admissible = previous.approved_admissible if previous else ZERO
        block_id, blocked = await self._block(claim_id, previous)
        decided = _money(decision.get("final_amount") or 0)
        admissible = await self._admissible(claim_id, decided)
        status: AuthorisationStatus = "awaiting_discharge"
        due: datetime | None = None
        if request in RAISES_BLOCK:
            if final == "reject":
                total, total_admissible = before, before_admissible
                if request is RequestType.PREAUTH:
                    total, total_admissible, status = ZERO, ZERO, "rejected"
            else:  # an issued authorisation is never lowered
                total = max(before, decided)
                total_admissible = max(before_admissible, admissible)
            if status == "rejected":
                if block_id is not None:
                    await self._money.release_block(
                        block_id=block_id,
                        utilised=ZERO,
                        idempotency_key=f"{claim_id}:release:preauth-reject",
                    )
                blocked = ZERO
            elif block_id is not None and total > 0 and total != blocked:
                ref = await self._money.adjust_block(
                    block_id=block_id,
                    amount=total,
                    idempotency_key=f"{claim_id}:adjust:{stored.version}",
                )
                blocked = ref.amount
        else:  # the discharge: the final authorisation; settlement releases the block
            total, status = (ZERO if final == "reject" else decided), "final"
            total_admissible = ZERO if final == "reject" else admissible
            due = _at_midnight(await self._discharged(claim_id) or self._now().date())
            due += timedelta(days=ORIGINALS_COURIER_DAYS)
        authorisation = Authorisation(
            claim_id=claim_id,
            request_type=request,
            submission_id=claim.current_submission_id,
            decision_version=stored.version,
            final_decision=final,
            approved_now=total - before,
            previous_total=before,
            approved_total=total,
            approved_admissible=total_admissible,
            diagnosis_codes=previous.diagnosis_codes if previous else await self._codes(claim_id),
            block_id=block_id,
            blocked=blocked,
            status=status,
            originals_due_at=due,
            authorised_at=self._now(),
        )
        await self._store(authorisation, close=status == "rejected", originals_due_at=due)
        return authorisation

    async def latest_authorisation(self, claim_id: UUID) -> Authorisation | None:
        stored = await self._records.latest(claim_id, Stage.AUTHORISATION)
        return Authorisation.model_validate(stored.payload) if stored else None

    async def mark_awaiting_discharge(self, claim_id: UUID) -> None:
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is None:
                raise NotCashless(str(claim_id))
            if claim.status != CLOSED:
                claim.status, claim.status_changed_at = AWAITING_DISCHARGE, self._now()

    async def cancel(self, claim_id: UUID, submission_id: UUID) -> Authorisation:
        previous = await self.latest_authorisation(claim_id)
        if previous is not None and previous.status == "cancelled":
            return previous
        block_id, blocked = await self._block(claim_id, previous)
        if block_id is not None and blocked > 0:
            await self._money.release_block(
                block_id=block_id, utilised=ZERO, idempotency_key=f"{claim_id}:release:cancel"
            )
        before = previous.approved_total if previous else ZERO
        cancelled = Authorisation(
            claim_id=claim_id,
            request_type=RequestType.CANCELLATION,
            submission_id=submission_id,
            decision_version=None,
            final_decision=None,
            approved_now=ZERO - before,
            previous_total=before,
            approved_total=ZERO,
            approved_admissible=ZERO,
            diagnosis_codes=previous.diagnosis_codes if previous else [],
            block_id=block_id,
            blocked=ZERO,
            status="cancelled",
            authorised_at=self._now(),
        )
        await self._store(cancelled, close=True)
        return cancelled

    async def mark_originals_received(self, claim_id: UUID, user_id: UUID) -> OriginalsStatus:
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is None or claim.originals_due_at is None:
                raise NoOriginalsDue(f"claim {claim_id} has no originals due")
            if claim.originals_received_at is None:
                claim.originals_received_at, claim.originals_received_by = self._now(), user_id
            return OriginalsStatus(
                claim_id=claim_id,
                due_at=claim.originals_due_at,
                received_at=claim.originals_received_at,
                received_by=claim.originals_received_by,
            )

    # --- Helpers -------------------------------------------------------------------------

    async def _claim(self, claim_id: UUID) -> ClaimRow:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
        if claim is None or claim.request_type != RequestType.PREAUTH.value:
            raise NotCashless(f"claim {claim_id} was not opened by a pre-auth")
        return claim

    async def _block(
        self, claim_id: UUID, previous: Authorisation | None
    ) -> tuple[str | None, Decimal]:
        """The block the claim holds: as the last authorisation left it, else as
        registration made it."""
        if previous is not None:
            return previous.block_id, previous.blocked
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        ref = (registered.payload.get("block_ref") if registered else None) or None
        if ref is None:
            return None, ZERO
        return str(ref["block_id"]), _money(ref["amount"])

    async def _admissible(self, claim_id: UUID, decided: Decimal) -> Decimal:
        """The admissible total (before co-pay) the decided amount stands for: the sheet's,
        scaled when the approver paid less than the sheet's payable."""
        sheet = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if sheet is None:
            return decided
        admissible = _money(sheet.payload.get("admissible_total") or 0)
        payable = _money(sheet.payload.get("payable") or 0)
        if payable <= 0 or decided >= payable:
            return admissible
        return _money(admissible * decided / payable)

    async def _codes(self, claim_id: UUID) -> list[str]:
        coded = await self._records.latest(claim_id, Stage.MEDICAL_CODING)
        if coded is None:
            return []
        return [v["code"]["code"] for v in coded.payload.get("verified_codes", [])]

    async def _discharged(self, claim_id: UUID) -> date | None:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        if digitised is None:
            return None
        value = ((digitised.payload.get("admission") or {}).get("discharge_date") or {}).get(
            "value"
        )
        return date.fromisoformat(value) if value else None

    async def _store(
        self,
        authorisation: Authorisation,
        *,
        close: bool,
        originals_due_at: datetime | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, authorisation.claim_id, with_for_update=True)
            assert claim is not None
            if close:
                now = self._now()
                claim.status, claim.status_changed_at, claim.closed_at = CLOSED, now, now
            if originals_due_at is not None:
                claim.originals_due_at = originals_due_at
            await append_in_session(
                session,
                authorisation.claim_id,
                Stage.AUTHORISATION,
                authorisation.model_dump(mode="json"),
                created_by="code",
            )


def _at_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)
