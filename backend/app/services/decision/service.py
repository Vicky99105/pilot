"""Default decision service: authority routing from config/authority_matrix.yaml, approver
and CRC outcomes checked in code and stored as versioned stage records."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import UserRoleRow, utcnow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.adjudication.contract import DraftDecision
from app.services.decision.contract import (
    PAID,
    ApproverOutcome,
    CrcOutcome,
    CrcReview,
    DecisionRecord,
    FinalDecision,
    InvalidDecision,
    NotAuthorised,
    NotReadyForDecision,
    Override,
    Routing,
)
from app.shared.types import Role, Stage

ZERO = Decimal("0.00")
# Stages an approver or the CRC may send a claim back to: any stage before the decision.
SEND_BACK_STAGES = (
    Stage.CLASSIFICATION,
    Stage.DIGITISATION,
    Stage.REGISTRATION,
    Stage.BILL_REVIEW,
    Stage.MEDICAL_REVIEW,
    Stage.RISK,
    Stage.ADJUDICATION,
)


class Band(BaseModel):
    name: str
    up_to: Decimal | None  # None: no upper limit


class AuthorityMatrix(BaseModel):
    version: int
    role: Role
    bands: list[Band]
    crc_required_for: list[str]

    def band_for(self, amount: Decimal) -> Band:
        for band in self.bands:
            if band.up_to is None or amount <= band.up_to:
                return band
        raise ValueError(f"no band covers Rs {amount}; the last band needs up_to: null")


def load_matrix(path: Path) -> AuthorityMatrix:
    return AuthorityMatrix.model_validate(yaml.safe_load(path.read_text()))


class DefaultDecisionService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        matrix_path: Path,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._matrix = load_matrix(matrix_path)
        self._records = StageRecordRepository(sessions)
        self._now = now

    async def route(self, claim_id: UUID) -> Routing:
        draft, version = await self._draft(claim_id)
        amount = draft.payable_on_sheet
        return Routing(
            claim_id=claim_id,
            adjudication_version=version,
            draft_decision=draft.decision,
            draft_guardrail_passed=draft.guardrail_passed,
            amount=amount,
            band=self._matrix.band_for(amount).name,
            role=self._matrix.role,
            authority_amount=amount,
            crc_required=draft.decision in self._matrix.crc_required_for,
        )

    async def record(self, claim_id: UUID, outcome: ApproverOutcome) -> DecisionRecord:
        latest = await self.latest(claim_id)
        if latest is not None and outcome.task_id and latest.task_id == outcome.task_id:
            return latest
        routing = await self.route(claim_id)
        draft, _ = await self._draft(claim_id)
        await self._authorise(outcome.approver_id, self._matrix.role, routing.authority_amount)

        final: FinalDecision | None = None
        amount, overrides = ZERO, list[Override]()
        if outcome.action == "send_back":
            _check_send_back(outcome.stage, outcome.note)
        elif outcome.action == "approve":
            if draft.decision == "query" or not draft.guardrail_passed:
                raise InvalidDecision(
                    f"the draft ({draft.decision}, guardrail passed: {draft.guardrail_passed}) "
                    "cannot be approved as it stands; override it or send it back"
                )
            final, amount = draft.decision, draft.amount
        else:
            final, amount, overrides = _override(draft, outcome)

        record = DecisionRecord(
            claim_id=claim_id,
            routing=routing,
            action=outcome.action,
            approver_id=outcome.approver_id,
            task_id=outcome.task_id,
            final_decision=final,
            final_amount=amount,
            overrides=overrides,
            reason=outcome.reason.strip(),
            send_back_stage=outcome.stage if outcome.action == "send_back" else None,
            send_back_note=outcome.note.strip(),
            crc_required=final is not None and final in self._matrix.crc_required_for,
            decided_at=self._now(),
        )
        return await self._store(record, created_by=f"user:{outcome.approver_id}")

    async def review_crc(self, claim_id: UUID, outcome: CrcOutcome) -> DecisionRecord:
        latest = await self.latest(claim_id)
        if latest is None:
            raise NotReadyForDecision(f"claim {claim_id} has no recorded decision")
        if latest.crc is not None and outcome.task_id and latest.crc.task_id == outcome.task_id:
            return latest
        if not latest.crc_required or latest.crc is not None:
            raise InvalidDecision(
                f"the latest decision ({latest.final_decision}) has no CRC review due"
            )
        await self._authorise(outcome.reviewer_id, Role.CRC_REVIEWER, None)
        if outcome.action == "send_back":
            _check_send_back(outcome.stage, outcome.note)
        record = latest.model_copy(
            update={
                "crc": CrcReview(
                    reviewer_id=outcome.reviewer_id,
                    task_id=outcome.task_id,
                    action=outcome.action,
                    send_back_stage=outcome.stage if outcome.action == "send_back" else None,
                    note=outcome.note.strip(),
                    reviewed_at=self._now(),
                )
            }
        )
        return await self._store(record, created_by=f"user:{outcome.reviewer_id}")

    async def latest(self, claim_id: UUID) -> DecisionRecord | None:
        stored = await self._records.latest(claim_id, Stage.DECISION)
        return DecisionRecord.model_validate(stored.payload) if stored else None

    async def _draft(self, claim_id: UUID) -> tuple[DraftDecision, int]:
        stored = await self._records.latest(claim_id, Stage.ADJUDICATION)
        if stored is None:
            raise NotReadyForDecision(f"claim {claim_id} has no adjudication draft")
        return DraftDecision.model_validate(stored.payload), stored.version

    async def _authorise(self, user_id: UUID, role: Role, amount: Decimal | None) -> None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(UserRoleRow).where(
                    UserRoleRow.user_id == user_id, UserRoleRow.role == role.value
                )
            )
        if row is None:
            raise NotAuthorised(f"user {user_id} does not hold the {role.value} role")
        if amount is not None and row.max_amount is not None and amount > row.max_amount:
            raise NotAuthorised(
                f"Rs {amount:,.2f} is above the user's approval limit of Rs {row.max_amount:,.2f}"
            )

    async def _store(self, record: DecisionRecord, *, created_by: str) -> DecisionRecord:
        await self._records.append(
            record.claim_id, Stage.DECISION, record.model_dump(mode="json"), created_by=created_by
        )
        return record


def _check_send_back(stage: Stage | None, note: str) -> None:
    if stage not in SEND_BACK_STAGES:
        names = ", ".join(s.value for s in SEND_BACK_STAGES)
        raise InvalidDecision(f"a send-back must name an earlier stage ({names}), not {stage}")
    if not note.strip():
        raise InvalidDecision("a send-back needs a note saying what to change")


def _override(
    draft: DraftDecision, outcome: ApproverOutcome
) -> tuple[FinalDecision, Decimal, list[Override]]:
    if not outcome.reason.strip():
        raise InvalidDecision("an override needs a reason")
    final = outcome.decision
    if final is None:
        if draft.decision == "query":
            raise InvalidDecision("the draft is a query; an override must name the decision")
        final = draft.decision
    if final in PAID:
        amount = outcome.amount if outcome.amount is not None else draft.payable_on_sheet
        if amount <= ZERO:
            raise InvalidDecision(f"a {final} must pay a positive amount")
        if amount > draft.payable_on_sheet:
            raise InvalidDecision(
                f"Rs {amount:,.2f} is more than the sheet payable Rs {draft.payable_on_sheet:,.2f}"
            )
        if amount > draft.available_balance:
            raise InvalidDecision(
                f"Rs {amount:,.2f} exceeds the available balance Rs {draft.available_balance:,.2f}"
            )
    else:
        if outcome.amount not in (None, ZERO):
            raise InvalidDecision("a reject pays nothing")
        amount = ZERO
    overrides = []
    if final != draft.decision:
        overrides.append(Override(field="decision", before=draft.decision, after=final))
    if amount != draft.amount:
        overrides.append(Override(field="amount", before=_text(draft.amount), after=_text(amount)))
    if not overrides:
        raise InvalidDecision("the override changes nothing; approve the draft instead")
    return final, amount, overrides


def _text(value: Any) -> str:
    return f"{value:.2f}"
