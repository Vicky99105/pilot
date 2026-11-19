"""Default settlement: payee checks, then (after the finance release) the payment instruction,
the block release and the claim's closure, through the MoneyService contract."""

import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, HospitalAgreementRow, UserRoleRow, utcnow
from app.database.repositories.stage_records import StageRecordRepository, append_in_session
from app.integrations.core.contract import MoneyService, Payee
from app.services.decision.contract import PAID, DecisionRecord
from app.services.digitisation.contract import ClaimRecord
from app.services.intake.contract import ClaimForm
from app.services.registration.contract import RegistrationResult
from app.services.settlement._helpers import digits, payee_checks
from app.services.settlement.contract import (
    IFSC_PATTERN,
    NotReadyForSettlement,
    PayeeCheck,
    PayeeCheckFailed,
    PaymentNotReleased,
    SettlementRecord,
)
from app.shared.types import CASHLESS_REQUESTS, RequestType, Role, Stage

ZERO = Decimal("0.00")
CLOSED = "closed"


class DefaultSettlementService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        money: MoneyService,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._money = money
        self._records = StageRecordRepository(sessions)
        self._now = now

    async def check_payee(self, claim_id: UUID) -> SettlementRecord:
        decision, version = await self._decision(claim_id)
        stored = await self._latest(claim_id, version)
        if stored is not None:
            return stored
        registration = await self._registration(claim_id)
        payee: Payee | None = None
        checks: list[PayeeCheck] = []
        claim = await self._claim(claim_id)
        cashless = RequestType(claim.request_type) in CASHLESS_REQUESTS
        previously_paid = await self._records.settled_paid(claim_id)
        decided = decision.final_amount if decision.final_decision in PAID else ZERO
        to_pay = max(decided - previously_paid, ZERO)
        if to_pay > 0 and cashless:
            payee, checks = await self._hospital_payee(claim)
        elif to_pay > 0:
            record = await self._claim_record(claim_id)
            checks = payee_checks(
                record.bank,
                record.claim_form_bank,
                registration.member.name,
                await self._submit_form(claim_id),
            )
            payee = Payee(
                kind="member",
                name=record.bank.account_holder.value or "",
                account_number=digits(record.bank.account_number.value),
                ifsc=(record.bank.ifsc.value or "").strip().upper(),
            )
        assert decision.final_decision is not None
        settlement = SettlementRecord(
            claim_id=claim_id,
            decision_version=version,
            final_decision=decision.final_decision,
            decided_amount=decision.final_amount,
            previously_paid=previously_paid,
            paid_amount=to_pay,
            payee=payee,
            payee_checks=checks,
            status="awaiting_release",
            released_by=None,
            payment_ref=None,
            block_id=await self._block_id(claim_id, registration),
            utilised=ZERO,
            released=ZERO,
            closed_at=None,
        )
        await self._records.append(
            claim_id, Stage.SETTLEMENT, settlement.model_dump(mode="json"), created_by="code"
        )
        return settlement

    async def settle(self, claim_id: UUID, *, released_by: UUID | None) -> SettlementRecord:
        checked = await self.check_payee(claim_id)
        if checked.status == "settled":
            return checked
        registration = await self._registration(claim_id)
        paid = checked.paid_amount > 0
        payment_ref = None
        if paid:
            if released_by is None or not await self._is_finance(released_by):
                raise PaymentNotReleased(
                    f"claim {claim_id}: a paid claim needs a release by a finance user"
                )
            failed = [c.check_id for c in checked.payee_checks if not c.passed]
            if failed:
                raise PayeeCheckFailed(f"payee checks failed: {', '.join(failed)}")
            assert checked.payee is not None
            payment_ref = await self._money.create_payment_instruction(
                payee=checked.payee,
                amount=checked.paid_amount,
                claim_number=registration.claim_number,
                idempotency_key=f"{claim_id}pay{checked.decision_version}",
            )
        utilised = checked.paid_amount if paid else ZERO
        released, closed_at = ZERO, self._now()
        block = await self._block(claim_id, registration)
        if block is not None:
            block_id, blocked = block
            await self._money.release_block(
                block_id=block_id,
                utilised=utilised,
                idempotency_key=f"{claim_id}release{checked.decision_version}",
            )
            released = blocked - utilised
        settled = checked.model_copy(
            update={
                "status": "settled",
                "released_by": released_by if paid else None,
                "payment_ref": payment_ref,
                "utilised": utilised,
                "released": released,
                "closed_at": closed_at,
            }
        )
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            assert claim is not None
            claim.status, claim.status_changed_at, claim.closed_at = CLOSED, closed_at, closed_at
            await append_in_session(
                session,
                claim_id,
                Stage.SETTLEMENT,
                settled.model_dump(mode="json"),
                created_by=f"user:{released_by}" if paid else "code",
            )
        return settled

    async def _claim(self, claim_id: UUID) -> ClaimRow:
        async with self._sessions() as session:
            claim = await session.get(ClaimRow, claim_id)
        if claim is None:
            raise NotReadyForSettlement(f"claim {claim_id} not found")
        return claim

    async def _block(
        self, claim_id: UUID, registration: RegistrationResult
    ) -> tuple[str, Decimal] | None:
        """The block to release: as the last cashless authorisation left it (an enhancement
        raises it), else as registration made it."""
        authorised = await self._records.latest(claim_id, Stage.AUTHORISATION)
        if authorised is not None and authorised.payload.get("block_id"):
            payload = authorised.payload
            return str(payload["block_id"]), Decimal(str(payload["blocked"]))
        if registration.block_ref is None:
            return None
        return registration.block_ref.block_id, registration.block_ref.amount

    async def _block_id(self, claim_id: UUID, registration: RegistrationResult) -> str | None:
        block = await self._block(claim_id, registration)
        return block[0] if block else None

    async def _hospital_payee(self, claim: ClaimRow) -> tuple[Payee | None, list[PayeeCheck]]:
        """A cashless claim pays the hospital the account its agreement names."""
        async with self._sessions() as session:
            agreement = await session.scalar(
                select(HospitalAgreementRow)
                .where(HospitalAgreementRow.hospital_id == claim.hospital_id)
                .order_by(HospitalAgreementRow.valid_from.desc())
                .limit(1)
            )
        account = digits(agreement.payee_account_number) if agreement else ""
        ifsc = ((agreement.payee_ifsc if agreement else None) or "").strip().upper()
        checks = [
            PayeeCheck(
                check_id="ifsc_format",
                passed=re.fullmatch(IFSC_PATTERN, ifsc) is not None,
                detail=f"IFSC {ifsc or '(none)'} against {IFSC_PATTERN}",
            ),
            PayeeCheck(
                check_id="agreement_payee_on_file",
                passed=bool(agreement and agreement.payee_name and account),
                detail=f"hospital {claim.hospital_id}: agreement account {account or '(none)'}",
            ),
        ]
        if agreement is None or not agreement.payee_name:
            return None, checks
        payee = Payee(kind="hospital", name=agreement.payee_name, account_number=account, ifsc=ifsc)
        return payee, checks

    async def _latest(self, claim_id: UUID, decision_version: int) -> SettlementRecord | None:
        stored = await self._records.latest(claim_id, Stage.SETTLEMENT)
        if stored is None or stored.payload["decision_version"] != decision_version:
            return None
        return SettlementRecord.model_validate(stored.payload)

    async def _decision(self, claim_id: UUID) -> tuple[DecisionRecord, int]:
        stored = await self._records.latest(claim_id, Stage.DECISION)
        if stored is None:
            raise NotReadyForSettlement(f"claim {claim_id} has no decision")
        decision = DecisionRecord.model_validate(stored.payload)
        if decision.final_decision is None:
            raise NotReadyForSettlement(f"claim {claim_id} was sent back; no final decision")
        if decision.crc_required and (decision.crc is None or decision.crc.action != "uphold"):
            raise NotReadyForSettlement(f"the {decision.final_decision} awaits the CRC")
        return decision, stored.version

    async def _registration(self, claim_id: UUID) -> RegistrationResult:
        stored = await self._records.latest(claim_id, Stage.REGISTRATION)
        if stored is None:
            raise NotReadyForSettlement(f"claim {claim_id} is not registered")
        return RegistrationResult.model_validate(stored.payload)

    async def _claim_record(self, claim_id: UUID) -> ClaimRecord:
        stored = await self._records.latest(claim_id, Stage.DIGITISATION)
        if stored is None:
            raise NotReadyForSettlement(f"claim {claim_id} has no extracted record")
        return ClaimRecord.model_validate(stored.payload)

    async def _submit_form(self, claim_id: UUID) -> ClaimForm | None:
        """The form typed in the Submit area, from the claim's intake record."""
        stored = await self._records.latest(claim_id, Stage.INTAKE)
        if stored is None or "form" not in stored.payload:
            return None
        return ClaimForm.model_validate(stored.payload["form"])

    async def _is_finance(self, user_id: UUID) -> bool:
        async with self._sessions() as session:
            row = await session.scalar(
                select(UserRoleRow).where(
                    UserRoleRow.user_id == user_id, UserRoleRow.role == Role.FINANCE.value
                )
            )
        return row is not None
