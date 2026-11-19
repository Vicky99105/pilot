"""Default bill review: head mapping in code, then R11, R13 and R21 in the rules engine."""

import copy
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import NonPayableItemRow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.bill_review._helpers import ZERO, map_head, money, room_rate_per_day
from app.services.bill_review.contract import (
    DeductionLine,
    DeductionSheet,
    InvalidEdit,
    LineEdit,
    NotReadyForBillReview,
)
from app.services.digitisation.contract import ClaimRecord
from app.services.registration.contract import RegistrationResult
from app.services.rules.contract import (
    Facts,
    Head,
    LineFacts,
    NonPayableItem,
    ProductConfig,
    RoomFacts,
    RulesEngine,
)
from app.shared.types import RuleResult, Stage

ONE_DAY = Decimal("1")


class DefaultBillReviewService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], rules: RulesEngine) -> None:
        self._sessions = sessions
        self._rules = rules
        self._records = StageRecordRepository(sessions)

    async def review(self, claim_id: UUID) -> DeductionSheet:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        if digitised is None or registered is None:
            raise NotReadyForBillReview(f"claim {claim_id} is not digitised and registered")
        latest = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if latest is not None and (
            latest.payload["digitisation_version"],
            latest.payload["registration_version"],
        ) == (digitised.version, registered.version):
            return DeductionSheet.model_validate(latest.payload)

        record = ClaimRecord.model_validate(digitised.payload)
        registration = RegistrationResult.model_validate(registered.payload)
        if registration.policy is None:
            raise NotReadyForBillReview(f"claim {claim_id} has no policy to review against")
        product = self._rules.product(registration.policy.product_uin)
        non_payable = await self._non_payable()

        lines: list[DeductionLine] = []
        days: dict[int, Decimal] = {}
        line_facts: dict[int, LineFacts] = {}
        results: list[RuleResult] = []
        for bill_line in record.bill.lines:
            head, flagged = map_head(bill_line.head.value, bill_line.description.value)
            claimed = money(bill_line.amount.value or ZERO)
            qty = bill_line.qty.value
            facts = LineFacts(
                line_id=bill_line.line_id,
                head=head,
                description=bill_line.description.value or "",
                qty=qty,
                rate=bill_line.rate.value,
                claimed=claimed,
            )
            line_facts[facts.line_id] = facts
            days[facts.line_id] = qty if qty and qty > 0 else ONE_DAY
            line = DeductionLine(
                line_id=facts.line_id,
                head=head,
                head_as_written=bill_line.head.value,
                description=facts.description,
                claimed=claimed,
                admissible=claimed,
                deducted=ZERO,
                rule_id=None,
                clause_ref=None,
                reason="no known head; check this line" if flagged else None,
                flagged=flagged,
            )
            [r11] = self._rules.evaluate(
                ["R11"], Facts(line=facts, non_payable=non_payable), product
            )
            results.append(r11)
            if not r11.passed:
                _deduct(line, r11)
            lines.append(line)

        # Non-payable lines (for example an admission kit under room charges) do not set
        # the room rate.
        payable_lines = [ln for ln in lines if ln.rule_id is None]
        room = RoomFacts(
            sum_insured=registration.policy.sum_insured,
            room_rate_per_day=room_rate_per_day(payable_lines, days),
            differential_billing=registration.hospital.differential_billing,
        )
        for line in payable_lines:
            [r13] = self._rules.evaluate(
                ["R13"], Facts(line=line_facts[line.line_id], room=room), product
            )
            results.append(r13)
            if r13.amount_effect != 0:
                _deduct(line, r13)

        sheet = _sheet(
            claim_id,
            digitised.version,
            registered.version,
            product,
            lines,
            results,
            self._rules,
        )
        await self._records.append(
            claim_id, Stage.BILL_REVIEW, sheet.model_dump(mode="json"), created_by="code"
        )
        return sheet

    async def apply_edits(
        self, claim_id: UUID, edits: list[LineEdit], *, actor: str
    ) -> DeductionSheet:
        latest = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if latest is None:
            raise InvalidEdit(f"claim {claim_id} has no deduction sheet to edit")
        current = DeductionSheet.model_validate(copy.deepcopy(latest.payload))
        found = {ln.line_id: ln for ln in current.lines}
        for edit in edits:
            line = found.get(edit.line_id)
            reason = edit.reason.strip()
            if line is None:
                raise InvalidEdit(f"no line {edit.line_id} on the sheet")
            if not reason:
                raise InvalidEdit(f"line {edit.line_id}: a reason is required")
            if not ZERO <= edit.admissible <= line.claimed:
                raise InvalidEdit(
                    f"line {edit.line_id}: admissible {edit.admissible} is outside "
                    f"0 to {line.claimed}"
                )
            line.admissible = edit.admissible
            line.deducted = money(line.claimed - edit.admissible)
            line.reason, line.judged_by = reason, "person"
        product = self._rules.product(current.product_uin)
        line_results = [r for r in current.rule_results if r.rule_id != "R21"]
        sheet = _sheet(
            claim_id,
            current.digitisation_version,
            current.registration_version,
            product,
            current.lines,
            line_results,
            self._rules,
        )
        await self._records.append(
            claim_id, Stage.BILL_REVIEW, sheet.model_dump(mode="json"), created_by=actor
        )
        return sheet

    async def _non_payable(self) -> list[NonPayableItem]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(NonPayableItemRow).order_by(
                    NonPayableItemRow.list_name, NonPayableItemRow.item_number
                )
            )
            return [
                NonPayableItem(
                    list_name=r.list_name,
                    item_number=r.item_number,
                    item=r.item,
                    condition=r.condition,
                )
                for r in rows
            ]


def _deduct(line: DeductionLine, result: RuleResult) -> None:
    line.admissible = money(max(line.claimed + result.amount_effect, ZERO))
    line.deducted = money(line.claimed - line.admissible)
    line.rule_id, line.clause_ref, line.reason = result.rule_id, result.clause_ref, result.reason


def _sheet(
    claim_id: UUID,
    digitisation_version: int,
    registration_version: int,
    product: ProductConfig,
    lines: list[DeductionLine],
    line_results: list[RuleResult],
    rules: RulesEngine,
) -> DeductionSheet:
    """Totals, then the co-pay last, on the admissible total."""
    admissible = money(sum((ln.admissible for ln in lines), ZERO))
    [r21] = rules.evaluate(["R21"], Facts(admissible_total=admissible), product)
    co_pay = money(-r21.amount_effect)
    return DeductionSheet(
        claim_id=claim_id,
        digitisation_version=digitisation_version,
        registration_version=registration_version,
        product_uin=product.product_uin,
        lines=lines,
        rule_results=[*line_results, r21],
        claimed_total=money(sum((ln.claimed for ln in lines), ZERO)),
        admissible_total=admissible,
        co_pay=co_pay,
        payable=money(admissible - co_pay),
        flagged_lines=[ln.line_id for ln in lines if ln.head is Head.OTHER and ln.flagged],
    )
