"""Default bill review, package first: head mapping in code, two closed model calls (R12 and
the package choice), and every amount from the rules engine."""

import copy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow, NonPayableItemRow
from app.database.repositories.stage_records import StageRecordRepository
from app.services.bill_review._helpers import ZERO, claim_route, map_head, money
from app.services.bill_review._prompts import (
    NON_PAYABLE_PURPOSE,
    PACKAGE_PURPOSE,
    PAYABLE,
    item_label,
    line_field,
    package_messages,
    package_schema,
    procedure_field,
    readings_messages,
    readings_schema,
)
from app.services.bill_review.contract import (
    NO_PACKAGE,
    BillFinding,
    DeductionLine,
    DeductionSheet,
    InvalidEdit,
    LineDeduction,
    LineEdit,
    NotReadyForBillReview,
    PackageChoice,
    PackageMapping,
    PackageOption,
)
from app.services.digitisation.contract import ClaimRecord
from app.services.knowledge_base.contract import (
    HospitalAgreement,
    KnowledgeBase,
    NonPayableCandidate,
    TariffCandidate,
)
from app.services.medical_review.contract import MedicalCoding
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.registration.contract import RegistrationResult
from app.services.rules.contract import (
    ClaimRoute,
    DrugFacts,
    Facts,
    Head,
    LineFacts,
    MappedPackage,
    NonPayableItem,
    NonPayableJudgement,
    PackageFacts,
    PreAuthFacts,
    ProductConfig,
    RoomFacts,
    RulesEngine,
)
from app.shared.codes import code_in_ranges
from app.shared.types import RequestType, RuleResult, Stage

ONE_DAY = Decimal("1")
FIXTURE = "fixture"  # the hand-made non-payable subset seeded for the tracer
CGHS = "CGHS"
OWN_TARIFF = "OWN"
# Bill heads whose descriptions can name the procedure, so they help find the package.
PROCEDURE_HEADS = (Head.OT, Head.PROFESSIONAL_FEES, Head.PACKAGE)


@dataclass
class _Line:
    """A bill line under review: the sheet line and what the rules read from it."""

    sheet: DeductionLine
    qty: Decimal | None
    rate: Decimal | None
    service_date: date | None

    def facts(self) -> LineFacts:
        """The line as the next rule reads it: the amount still admissible, and the daily
        rate that amount implies once an earlier rule has reduced it."""
        line = self.sheet
        rate = self.rate
        if line.deductions and self.qty and self.qty > 0:
            rate = line.admissible / self.qty
        return LineFacts(
            line_id=line.line_id,
            head=line.head,
            description=line.description,
            qty=self.qty,
            rate=rate,
            claimed=line.admissible,
            service_date=self.service_date,
        )


class DefaultBillReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rules: RulesEngine,
        gateway: ModelGateway,
        kb: KnowledgeBase,
    ) -> None:
        self._sessions = sessions
        self._rules = rules
        self._gateway = gateway
        self._kb = kb
        self._records = StageRecordRepository(sessions)

    async def review(self, claim_id: UUID) -> DeductionSheet:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        coded = await self._records.latest(claim_id, Stage.MEDICAL_CODING)
        if digitised is None or registered is None or coded is None:
            raise NotReadyForBillReview(
                f"claim {claim_id} is not digitised, registered and medically coded"
            )
        latest = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if latest is not None and (
            latest.payload["digitisation_version"],
            latest.payload["registration_version"],
            latest.payload.get("coding_version"),
        ) == (digitised.version, registered.version, coded.version):
            return DeductionSheet.model_validate(latest.payload)

        record = ClaimRecord.model_validate(digitised.payload)
        registration = RegistrationResult.model_validate(registered.payload)
        coding = MedicalCoding.model_validate(coded.payload)
        if registration.policy is None:
            raise NotReadyForBillReview(f"claim {claim_id} has no policy to review against")
        product = self._rules.product(registration.policy.product_uin)
        route = await self._claim_route(claim_id)
        admitted = record.admission.admission_date.value
        hospital_id = registration.hospital.rohini_id
        agreement = (
            await self._kb.hospital_agreement(hospital_id, as_of=admitted) if hospital_id else None
        )
        review = _Review(claim_id, product, self._rules)
        review.lines = _lines(record)

        # 1. Per line: R11, R12, R18.
        non_payable = await self._non_payable()
        unmatched = []  # lines R11 matched no list item for: only these can be unclear
        for line in review.lines:
            r11 = review.apply("R11", line, Facts(line=line.facts(), non_payable=non_payable))
            if r11.clause_ref is None:
                unmatched.append(line)
        await self._judge_unclear_lines(review, unmatched)
        for line in review.payable():
            review.apply(
                "R18",
                line,
                Facts(
                    line=line.facts(),
                    admission_date=admitted,
                    discharge_date=record.admission.discharge_date.value,
                ),
            )

        # 2. Package: map, then R23 and R20. The package is mapped on every route (the full
        # medical review needs its guideline); the rules price by it only where the hospital
        # agreement binds the claim's route.
        mapping = await self._map_package(review, record, coding, agreement, hospital_id)
        package = None
        if mapping is not None and mapping.packages:
            package = PackageFacts(
                packages=mapping.packages, billed_as_package=mapping.billed_as_package
            )
            for line in review.lines:
                line.sheet.in_package = _in_package(line.sheet.head, package, product, route)
            for line in review.payable():
                review.apply(
                    "R23", line, Facts(line=line.facts(), package=package, claim_route=route)
                )
            priced = [
                ln
                for ln in review.payable()
                if ln.sheet.in_package
                and (ln.sheet.head is Head.PACKAGE or not package.billed_as_package)
            ]
            total = sum((ln.sheet.admissible for ln in priced), ZERO)
            facts = Facts(
                package=package.model_copy(update={"package_total": total}), claim_route=route
            )
            review.spread("R20", priced, facts)

        # 3. Per line outside the package: R24, R13, R14.
        discount = agreement.non_package_discount_percent if agreement else None
        for line in review.payable():
            review.apply(
                "R24",
                line,
                Facts(
                    line=line.facts(),
                    package=package,
                    non_package_discount_percent=discount,
                    claim_route=route,
                ),
            )
        room = RoomFacts(
            sum_insured=registration.policy.sum_insured,
            room_rate_per_day=_room_rate(review.payable()),
            differential_billing=registration.hospital.differential_billing,
        )
        for line in review.payable():
            for rule_id in ("R13", "R14"):
                review.apply(
                    rule_id,
                    line,
                    Facts(line=line.facts(), room=room, package=package, claim_route=route),
                )

        # 4. Claim: R19, R16, R17, R26.
        ambulance = [ln for ln in review.payable() if ln.sheet.head is Head.AMBULANCE]
        if ambulance:
            total = sum((ln.sheet.admissible for ln in ambulance), ZERO)
            review.spread("R19", ambulance, Facts(ambulance_total=total))
        procedures = [p.code.code for p in coding.procedure_codes]
        diagnoses = [v.code.code for v in coding.verified_codes]
        preauth = await self._approved(claim_id) if record.bill.lines else None
        for rule_id in ("R16", "R17", "R26"):
            review.spread(
                rule_id,
                review.payable(),
                Facts(
                    sum_insured=registration.policy.sum_insured,
                    admissible_total=review.admissible(),
                    procedure_codes=procedures,
                    diagnosis_codes=diagnoses,
                    preauth=preauth,
                ),
            )

        await self._check_drug_prices(review)
        sheet = _sheet(
            claim_id,
            digitised.version,
            registered.version,
            product,
            [ln.sheet for ln in review.lines],
            review.results,
            self._rules,
        ).model_copy(
            update={
                "coding_version": coded.version,
                "agreement": agreement,
                "package": mapping,
                "findings": review.findings,
            }
        )
        created_by = "model" if review.model_used else "code"
        await self._records.append(
            claim_id, Stage.BILL_REVIEW, sheet.model_dump(mode="json"), created_by=created_by
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
        ).model_copy(
            update={
                "coding_version": current.coding_version,
                "agreement": current.agreement,
                "package": current.package,
                "findings": current.findings,
            }
        )
        await self._records.append(
            claim_id, Stage.BILL_REVIEW, sheet.model_dump(mode="json"), created_by=actor
        )
        return sheet

    async def _approved(self, claim_id: UUID) -> PreAuthFacts | None:
        """R26 at discharge: what the pre-auth and enhancements approved, before co-pay,
        and the diagnosis codes the pre-auth verified. None on a claim with no pre-auth."""
        stored = await self._records.latest(claim_id, Stage.AUTHORISATION)
        if stored is None or stored.payload.get("approved_admissible") is None:
            return None
        return PreAuthFacts(
            approved_total=Decimal(str(stored.payload["approved_admissible"])),
            diagnosis_codes=stored.payload.get("diagnosis_codes") or [],
        )

    async def _claim_route(self, claim_id: UUID) -> ClaimRoute | None:
        async with self._sessions() as session:
            request_type = await session.scalar(
                select(ClaimRow.request_type).where(ClaimRow.id == claim_id)
            )
        return claim_route(RequestType(request_type)) if request_type else None

    # --- R12 -----------------------------------------------------------------------------

    async def _judge_unclear_lines(self, review: "_Review", unmatched: list[_Line]) -> None:
        """Lines R11 matched no list item for that may still be one: one closed model call
        for every distinct description, then R12 on each line."""
        unclear: dict[str, list[NonPayableCandidate]] = {}
        for line in unmatched:
            text = line.sheet.description
            if text not in unclear:
                unclear[text] = await self._kb.match_non_payable(text)
        unclear = {text: found for text, found in unclear.items() if found}
        if not unclear:
            return
        texts = list(unclear)
        heads = {ln.sheet.description: ln.sheet.head.value for ln in review.lines}
        reply = await self._gateway.structured(
            ModelRole.REASONING,
            readings_messages([(t, heads[t]) for t in texts], list(unclear.values())),
            readings_schema(list(unclear.values())),
            claim_id=review.claim_id,
            purpose=NON_PAYABLE_PURPOSE,
        )
        review.model_used = True
        readings = reply.model_dump()
        judgements: dict[str, NonPayableJudgement] = {}
        for index, text in enumerate(texts, start=1):
            reading = readings[line_field(index)]
            chosen = next((c for c in unclear[text] if item_label(c) == reading["choice"]), None)
            judgements[text] = NonPayableJudgement(
                item=None if reading["choice"] == PAYABLE or chosen is None else _item(chosen),
                confidence=float(reading["confidence"]),
            )
        for line in unmatched:
            judgement = judgements.get(line.sheet.description)
            if judgement is None:
                continue
            result = review.apply("R12", line, Facts(line=line.facts(), judgement=judgement))
            if result.amount_effect == 0 and not result.passed:
                line.sheet.flagged = True  # unsure: the bill processor decides
                line.sheet.flag_reason = result.reason

    # --- Package -------------------------------------------------------------------------

    async def _map_package(
        self,
        review: "_Review",
        record: ClaimRecord,
        coding: MedicalCoding,
        agreement: HospitalAgreement | None,
        hospital_id: str | None,
    ) -> PackageMapping | None:
        """The packages the verified procedures map to, priced for the agreement. None when
        no package can apply: no agreement, the hospital's own tariff, or no procedure."""
        if agreement is None or agreement.scheme == OWN_TARIFF or not coding.procedure_codes:
            return None
        procedures = [
            f"{p.phrase} ({p.code.code}: {p.code.display})" for p in coding.procedure_codes
        ]
        names = [p.phrase for p in coding.procedure_codes]
        names += [p.code.display for p in coding.procedure_codes if p.code.display]
        names += [ln.sheet.description for ln in review.lines if ln.sheet.head in PROCEDURE_HEADS]
        found = await self._kb.tariff_candidates([], list(dict.fromkeys(names)), hospital_id)
        candidates = [_option(c, agreement) for c in found if c.scheme == agreement.scheme]
        billed_as_package = any(ln.sheet.head is Head.PACKAGE for ln in review.lines)
        if not candidates:
            review.findings.append(
                BillFinding(
                    kind="package",
                    text=(
                        f"no {agreement.scheme} package matches the procedures "
                        f"({'; '.join(procedures)}); priced as non-package charges"
                    ),
                )
            )
            return PackageMapping(
                candidates=[], choices=[], packages=[], billed_as_package=billed_as_package
            )
        diagnoses = [f"{v.code.code} {v.code.display or ''}".strip() for v in coding.verified_codes]
        reply = await self._gateway.structured(
            ModelRole.REASONING,
            package_messages(procedures, diagnoses, candidates),
            package_schema(procedures, candidates),
            claim_id=review.claim_id,
            purpose=PACKAGE_PURPOSE,
        )
        review.model_used = True
        chosen = reply.model_dump()
        choices = [
            PackageChoice(procedure=p, chosen=chosen[procedure_field(i)])
            for i, p in enumerate(procedures, start=1)
        ]
        by_code = {c.package_code: c for c in candidates}
        packages: list[MappedPackage] = []
        for code in dict.fromkeys(c.chosen for c in choices if c.chosen != NO_PACKAGE):
            option = by_code[code]
            if option.rate is None:
                review.findings.append(
                    BillFinding(
                        kind="package",
                        text=(
                            f"package {code} has no {agreement.scheme} rate for "
                            f"{agreement.accreditation or 'this hospital'}, "
                            f"{agreement.package_ward or 'any'} ward; priced as non-package"
                        ),
                    )
                )
                continue
            packages.append(
                MappedPackage(
                    scheme=option.scheme,
                    package_code=code,
                    name=option.name,
                    rate=option.rate,
                    addon=option.addon,
                    standalone=option.standalone,
                )
            )
            review.findings += await self._diagnosis_fit(option, diagnoses_codes(coding))
        return PackageMapping(
            candidates=candidates,
            choices=choices,
            packages=packages,
            billed_as_package=billed_as_package,
        )

    async def _diagnosis_fit(self, option: PackageOption, codes: list[str]) -> list[BillFinding]:
        """A finding when our crosswalk does not list the package, or lists it for other
        diagnoses. Never a deduction: a person judges the fit."""
        crosswalk = await self._kb.package_crosswalk(option.scheme, option.package_code)
        if crosswalk is None:
            text = (
                f"package {option.package_code} {option.name!r} is not in the diagnosis "
                f"crosswalk; whether it fits {', '.join(codes) or 'the diagnoses'} is unknown"
            )
            return [BillFinding(kind="diagnosis_package", text=text)]
        if any(code_in_ranges(c, crosswalk.icd10cm) for c in codes):
            return []
        text = (
            f"package {option.package_code} {option.name!r} treats {', '.join(crosswalk.icd10cm)}"
            f"; the verified diagnoses are {', '.join(codes) or 'none'}"
        )
        return [BillFinding(kind="diagnosis_package", text=text)]

    # --- Drug prices ---------------------------------------------------------------------

    async def _check_drug_prices(self, review: "_Review") -> None:
        checked: dict[tuple[str, Decimal | None], DrugFacts | None] = {}
        for line in review.lines:
            if line.sheet.head is not Head.PHARMACY:
                continue
            unit_price = line.rate
            key = (line.sheet.description, unit_price)
            if key not in checked:
                check = await self._kb.check_drug(line.sheet.description, unit_price)
                match = next(
                    (m for m in check.matches if m.ceiling_price == check.ceiling_price), None
                )
                checked[key] = (
                    DrugFacts(
                        medicine=match.medicine,
                        dosage_form=match.dosage_form,
                        unit=match.unit,
                        ceiling_price=match.ceiling_price,
                    )
                    if match is not None and check.ceiling_price is not None
                    else None
                )
            drug = checked[key]
            if drug is None:
                continue
            billed = line.facts().model_copy(  # the price billed, before any deduction
                update={"claimed": line.sheet.claimed, "rate": line.rate}
            )
            [result] = self._rules.evaluate(
                ["DRUG-PRICE"], Facts(line=billed, drug=drug), review.product
            )
            if not result.passed:
                review.findings.append(
                    BillFinding(
                        kind="drug_price",
                        text=f"{line.sheet.description}: {result.reason}",
                        line_id=line.sheet.line_id,
                    )
                )

    async def _non_payable(self) -> list[NonPayableItem]:
        """The loaded wording's lists when one is loaded, else the fixture."""
        async with self._sessions() as session:
            loaded = await session.scalar(
                select(NonPayableItemRow.source_version)
                .where(NonPayableItemRow.source != FIXTURE)
                .order_by(NonPayableItemRow.id.desc())
                .limit(1)
            )
            query = select(NonPayableItemRow).order_by(
                NonPayableItemRow.list_name, NonPayableItemRow.item_number
            )
            if loaded is None:
                query = query.where(NonPayableItemRow.source == FIXTURE)
            else:
                query = query.where(
                    NonPayableItemRow.source != FIXTURE,
                    NonPayableItemRow.source_version == loaded,
                )
            rows = await session.scalars(query)
            return [
                NonPayableItem(
                    list_name=r.list_name,
                    item_number=r.item_number,
                    item=r.item,
                    condition=r.condition,
                    payable_in_product=r.payable_in_product,
                )
                for r in rows
            ]


class _Review:
    """The lines under review, the rule results so far and the findings."""

    def __init__(self, claim_id: UUID, product: ProductConfig, rules: RulesEngine) -> None:
        self.claim_id = claim_id
        self.product = product
        self.rules = rules
        self.lines: list[_Line] = []
        self.results: list[RuleResult] = []
        self.findings: list[BillFinding] = []
        self.model_used = False

    def payable(self) -> list[_Line]:
        return [ln for ln in self.lines if ln.sheet.admissible > 0]

    def admissible(self) -> Decimal:
        return money(sum((ln.sheet.admissible for ln in self.lines), ZERO))

    def apply(self, rule_id: str, line: _Line, facts: Facts) -> RuleResult:
        [result] = self.rules.evaluate([rule_id], facts, self.product)
        self.results.append(result)
        _deduct(line.sheet, result, -result.amount_effect)
        return result

    def spread(self, rule_id: str, lines: list[_Line], facts: Facts) -> RuleResult:
        """A claim-level rule, its amount spread over the lines in proportion."""
        [result] = self.rules.evaluate([rule_id], facts, self.product)
        self.results.append(result)
        if result.amount_effect != 0:
            shares = self.rules.allocate(
                result.amount_effect, [ln.sheet.admissible for ln in lines]
            )
            for line, share in zip(lines, shares, strict=True):
                _deduct(line.sheet, result, -share)
        return result


def diagnoses_codes(coding: MedicalCoding) -> list[str]:
    return [v.code.code for v in coding.verified_codes]


def _estimate_lines(record: ClaimRecord) -> list[_Line]:
    """A cashless request before discharge has no bill: its lines are the estimate, the
    Part C heads, then each enhancement's heads, mapped like bill lines. A head with days
    (room, ICU) carries them as its quantity."""
    sources = [("Pre-auth estimate", record.preauth.estimate)]
    sources += [(f"Enhancement {n}", e.estimate) for n, e in enumerate(record.enhancements, 1)]
    lines: list[_Line] = []
    for written, heads in sources:
        for item in heads:
            head, flagged = map_head(item.head.value, None)
            claimed = money(item.amount.value or ZERO)
            days = item.days.value if item.days.value and item.days.value > 0 else None
            sheet = DeductionLine(
                line_id=len(lines) + 1,
                head=head,
                head_as_written=written,
                description=item.head.value or "",
                claimed=claimed,
                admissible=claimed,
                deducted=ZERO,
                rule_id=None,
                clause_ref=None,
                reason=None,
                flagged=flagged,
                flag_reason="no known head; check this line" if flagged else None,
            )
            rate = claimed / days if days else None
            lines.append(_Line(sheet=sheet, qty=days, rate=rate, service_date=None))
    return lines


def _lines(record: ClaimRecord) -> list[_Line]:
    if not record.bill.lines:
        return _estimate_lines(record)
    lines = []
    for bill_line in record.bill.lines:
        head, flagged = map_head(bill_line.head.value, bill_line.description.value)
        claimed = money(bill_line.amount.value or ZERO)
        qty = bill_line.qty.value
        sheet = DeductionLine(
            line_id=bill_line.line_id,
            head=head,
            head_as_written=bill_line.head.value,
            description=bill_line.description.value or "",
            claimed=claimed,
            admissible=claimed,
            deducted=ZERO,
            rule_id=None,
            clause_ref=None,
            reason=None,
            flagged=flagged,
            flag_reason="no known head; check this line" if flagged else None,
        )
        lines.append(
            _Line(
                sheet=sheet,
                qty=qty if qty and qty > 0 else None,
                rate=bill_line.rate.value,
                service_date=bill_line.date.value,
            )
        )
    return lines


def _item(candidate: NonPayableCandidate) -> NonPayableItem:
    return NonPayableItem(
        list_name=candidate.list_name,
        item_number=candidate.item_number,
        item=candidate.item,
        condition=candidate.condition,
        payable_in_product=candidate.payable_in_product,
    )


def _option(candidate: TariffCandidate, agreement: HospitalAgreement) -> PackageOption:
    """The candidate priced for the agreement: CGHS at the agreement's accreditation and
    ward, for the hospital's city tier; other schemes at their listed rate."""
    rate, basis = candidate.rate, candidate.rate_basis
    if candidate.scheme == CGHS:
        priced = next(
            (
                r
                for r in candidate.rates
                if r.accreditation == agreement.accreditation and r.ward == agreement.package_ward
            ),
            None,
        )
        rate = priced.rate if priced else None
        basis = (
            f"CGHS {agreement.accreditation}, {candidate.tier} city tier, "
            f"{agreement.package_ward} ward"
        )
    return PackageOption(
        scheme=candidate.scheme,
        package_code=candidate.procedure_code,
        procedure_code=candidate.procedure_code,
        name=candidate.name,
        rate=rate,
        rate_basis=basis,
        addon=bool(candidate.addon),
        standalone=bool(candidate.standalone),
        score=candidate.score,
    )


def _in_package(
    head: Head, package: PackageFacts, product: ProductConfig, route: ClaimRoute | None
) -> bool:
    """True when the package prices the line: the agreement binds the claim's route, and the
    line is a package charge or a head the package covers."""
    return (
        bool(package.packages)
        and route in product.packages.pricing_applies_to
        and (head is Head.PACKAGE or head in product.packages.included_heads)
    )


def _room_rate(lines: list[_Line]) -> Decimal | None:
    """The average daily rate charged across the room lines outside any package, after the
    earlier rules, or None without one."""
    room = [ln for ln in lines if ln.sheet.head is Head.ROOM and not ln.sheet.in_package]
    days = sum((ln.qty or ONE_DAY for ln in room), ZERO)
    if not room or days <= 0:
        return None
    return sum((ln.sheet.admissible for ln in room), ZERO) / days


def _deduct(line: DeductionLine, result: RuleResult, amount: Decimal) -> None:
    """Take amount off the line (never below zero) and record which rule took it."""
    take = money(min(max(amount, ZERO), line.admissible))
    if take == 0:
        return
    line.admissible = money(line.admissible - take)
    line.deducted = money(line.claimed - line.admissible)
    line.deductions.append(
        LineDeduction(
            rule_id=result.rule_id,
            amount=take,
            clause_ref=result.clause_ref,
            reason=result.reason,
            judged_by=result.judged_by,
        )
    )
    main = max(line.deductions, key=lambda d: d.amount)
    line.rule_id, line.clause_ref, line.reason = main.rule_id, main.clause_ref, main.reason
    line.judged_by = main.judged_by


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
