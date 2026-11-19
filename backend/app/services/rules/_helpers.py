"""The rules, one pure function each, and the registry that names them. No I/O."""

import re
from calendar import monthrange
from collections.abc import Callable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from rapidfuzz import fuzz

from app.integrations.core.contract import InsuredMember, MemberPolicy
from app.services.rules.contract import (
    PAID_FLAGS,
    Claimant,
    DrugFacts,
    Facts,
    Head,
    LineFacts,
    MappedPackage,
    MemberMatch,
    NonPayableItem,
    ProductConfig,
    RoomFacts,
)
from app.shared.codes import code_in_ranges, has_prefix
from app.shared.types import RuleResult

CENTS = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

NAME_MATCH = 90.0  # token_sort_ratio at or above this, with the same DOB, is the member
NAME_DOUBTFUL = 70.0  # at or above this, a person decides
NON_PAYABLE_MATCH = 92.0  # ratio at or above this is a near-exact item match
_TITLES = {"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "dr", "master", "baby", "kumari"}
_MONTHS = {"monthly": 1, "quarterly": 3, "half_yearly": 6}

Rule = Callable[[Facts, ProductConfig], RuleResult]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _rupees(value: Decimal) -> str:
    return f"Rs {money(value):,.2f}"


def _missing(rule_id: str, *names: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, passed=False, reason=f"missing facts: {', '.join(names)}")


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


# --- R1: premium received before cover start (Section 64VB) ------------------------------


def r1_premium_received(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.premium_received
    if facts.policy is None:
        return _missing("R1", "policy")
    start = facts.policy.policy_start
    received = [p for p in facts.policy.premium_payments if p.status == "received"]
    early = [p for p in received if p.paid_on <= start]
    if early:
        first = min(p.paid_on for p in early)
        reason = f"premium received on {first}, on or before cover start {start}"
        return RuleResult(rule_id="R1", passed=True, reason=reason, clause_ref=clause)
    if received:
        first = min(p.paid_on for p in received)
        reason = f"premium received on {first}, after cover start {start}"
    else:
        reason = "no received premium payment on record"
    return RuleResult(rule_id="R1", passed=False, reason=reason, clause_ref=clause)


# --- R2: policy in force or in grace on admission ----------------------------------------


def _paid_until(policy: MemberPolicy) -> date:
    """The last covered day: the policy end for annual premium, else the end of the last
    instalment period paid for."""
    months = _MONTHS.get(policy.premium_frequency)
    if months is None:
        return policy.policy_end
    paid = sum(1 for p in policy.premium_payments if p.status == "received")
    covered = add_months(policy.policy_start, paid * months) - timedelta(days=1)
    return min(covered, policy.policy_end)


def r2_in_force(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.policy is None or facts.admission_date is None:
        return _missing("R2", *_absent(policy=facts.policy, admission_date=facts.admission_date))
    policy, admitted = facts.policy, facts.admission_date
    clauses = product.clauses
    if admitted < policy.policy_start:
        reason = f"admitted {admitted}, before the policy starts on {policy.policy_start}"
        return RuleResult(
            rule_id="R2", passed=False, reason=reason, clause_ref=clauses.policy_period
        )
    paid_until = _paid_until(policy)
    if admitted <= paid_until:
        reason = f"admitted {admitted}, within cover {policy.policy_start} to {paid_until}"
        return RuleResult(
            rule_id="R2", passed=True, reason=reason, clause_ref=clauses.policy_period
        )
    days = product.grace_period_days
    grace = days.monthly if policy.premium_frequency == "monthly" else days.other
    grace_end = paid_until + timedelta(days=grace)
    passed = admitted <= grace_end
    state = "within" if passed else "after"
    reason = (
        f"admitted {admitted}, {state} the {grace}-day grace period after {paid_until} "
        f"(ends {grace_end})"
    )
    return RuleResult(rule_id="R2", passed=passed, reason=reason, clause_ref=clauses.grace_period)


def _absent(**values: object) -> list[str]:
    return [name for name, value in values.items() if value is None]


# --- R3: member match ---------------------------------------------------------------------


def normalise_name(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in _TITLES)


def _ident(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def match_member(claimant: Claimant, members: list[InsuredMember]) -> MemberMatch:
    if claimant.member_id:
        wanted = _ident(claimant.member_id)
        for member in members:
            if _ident(member.member_id) == wanted:
                return MemberMatch(
                    kind="exact_id",
                    member=member,
                    score=100.0,
                    reason=f"exact member ID {member.member_id}",
                )
    if not claimant.name or not members:
        return MemberMatch(kind="none", member=None, score=0.0, reason="no matching member")
    name = normalise_name(claimant.name)
    scored = sorted(
        ((fuzz.token_sort_ratio(name, normalise_name(m.name)), m) for m in members),
        key=lambda pair: pair[0],
        reverse=True,
    )
    score, best = scored[0]
    if score >= NAME_MATCH and claimant.dob is not None and claimant.dob == best.dob:
        return MemberMatch(
            kind="name_dob",
            member=best,
            score=score,
            reason=f"matched {best.member_id} by name and date of birth (name score {score:.0f})",
        )
    if score >= NAME_DOUBTFUL:
        dob = "date of birth missing" if claimant.dob is None else f"DOB {claimant.dob}"
        return MemberMatch(
            kind="doubtful",
            member=None,
            score=score,
            reason=(
                f"doubtful match: closest is {best.member_id} {best.name!r} born {best.dob} "
                f"(name score {score:.0f}, {dob})"
            ),
        )
    return MemberMatch(kind="none", member=None, score=score, reason="no matching member")


def r3_member_match(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.policy is None or facts.claimant is None:
        return _missing("R3", *_absent(policy=facts.policy, claimant=facts.claimant))
    match = match_member(facts.claimant, facts.policy.insured_members)
    return RuleResult(
        rule_id="R3",
        passed=match.member is not None,
        reason=match.reason,
        clause_ref=product.clauses.insured_person,
    )


# --- R5: 30-day initial waiting period ----------------------------------------------------


def r5_initial_wait(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.initial_waiting
    if facts.policy is None or facts.admission_date is None:
        return _missing("R5", *_absent(policy=facts.policy, admission_date=facts.admission_date))
    wait = product.initial_waiting
    admitted, policy = facts.admission_date, facts.policy
    if facts.accident and wait.accident_exempt:
        reason = "hospitalisation due to an accident is exempt from the initial wait"
        return RuleResult(rule_id="R5", passed=True, reason=reason, clause_ref=clause)
    waiver_from = add_months(
        policy.continuous_cover_start, wait.waived_after_continuous_cover_months
    )
    if admitted > waiver_from:
        reason = (
            f"continuous cover since {policy.continuous_cover_start} exceeds "
            f"{wait.waived_after_continuous_cover_months} months"
        )
        return RuleResult(rule_id="R5", passed=True, reason=reason, clause_ref=clause)
    days = (admitted - policy.inception_date).days
    passed = days >= wait.days
    reason = (
        f"admitted {days} days after inception on {policy.inception_date}; "
        f"the initial wait is {wait.days} days"
    )
    return RuleResult(rule_id="R5", passed=passed, reason=reason, clause_ref=clause)


# --- R10: sum insured available -----------------------------------------------------------


def available_sum_insured(policy: MemberPolicy) -> Decimal:
    return money(policy.sum_insured + policy.cumulative_bonus - policy.utilised - policy.blocked)


def r10_sum_insured(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.policy is None or facts.amount is None:
        return _missing("R10", *_absent(policy=facts.policy, amount=facts.amount))
    available = available_sum_insured(facts.policy)
    amount = money(facts.amount)
    passed = amount <= available
    reason = (
        f"available {_rupees(available)} (sum insured plus bonus, less utilised and blocked) "
        f"{'covers' if passed else 'is less than'} {_rupees(amount)}"
    )
    return RuleResult(
        rule_id="R10", passed=passed, reason=reason, clause_ref=product.clauses.sum_insured
    )


# --- R11: exact or near-exact non-payable match --------------------------------------------


def _item_key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def find_non_payable(description: str, items: list[NonPayableItem]) -> NonPayableItem | None:
    key = _item_key(description)
    if not key:
        return None
    best = max(items, key=lambda i: fuzz.ratio(key, _item_key(i.item)), default=None)
    if best is not None and fuzz.ratio(key, _item_key(best.item)) >= NON_PAYABLE_MATCH:
        return best
    return None


def r11_non_payable(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None:
        return _missing("R11", "line")
    line = facts.line
    item = find_non_payable(line.description, facts.non_payable)
    if item is None:
        return RuleResult(rule_id="R11", passed=True, reason="not on the non-payable lists")
    condition = f"; {item.condition}" if item.condition else ""
    clause = product.clauses.non_payable.get(item.list_name)
    if item.payable_in_product in PAID_FLAGS:
        return RuleResult(
            rule_id="R11",
            passed=True,
            reason=f"paid: List {item.list_name} item {item.item_number} {item.item!r}{condition}",
            clause_ref=clause,
        )
    return RuleResult(
        rule_id="R11",
        passed=False,
        amount_effect=-money(line.claimed),
        reason=(
            f"non-payable: List {item.list_name} item {item.item_number} {item.item!r}{condition}"
        ),
        clause_ref=clause,
    )


# --- R13: room and ICU caps with proportionate deduction ----------------------------------


def daily_cap(percent: Decimal, maximum: Decimal, sum_insured: Decimal) -> Decimal:
    return money(min(sum_insured * percent / HUNDRED, maximum))


def r13_room_cap(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None or facts.room is None:
        return _missing("R13", *_absent(line=facts.line, room=facts.room))
    line, room = facts.line, facts.room
    clauses = product.clauses
    if in_package(line.head, facts, product):
        return _priced_by_package("R13", facts)
    if line.head is Head.ICU:
        return RuleResult(rule_id="R13", passed=True, reason="an ICU line: R14 caps it")
    if line.head is Head.ROOM:
        return _daily_cap("R13", line, room, product)
    rules = product.proportionate_deduction
    if line.head not in rules.linked_heads:
        kind = "excluded from" if line.head in rules.excluded_heads else "not subject to"
        reason = f"{line.head.value} is {kind} proportionate deduction"
        return RuleResult(rule_id="R13", passed=True, reason=reason)
    clause = clauses.proportionate_deduction
    if rules.only_with_differential_billing and not room.differential_billing:
        reason = "hospital has no differential billing; no proportionate deduction"
        return RuleResult(rule_id="R13", passed=True, reason=reason, clause_ref=clause)
    cap = daily_cap(
        product.room_rent.percent_of_sum_insured, product.room_rent.max_per_day, room.sum_insured
    )
    room_rate = room.room_rate_per_day
    if room_rate is None or room_rate <= cap:
        return RuleResult(
            rule_id="R13", passed=True, reason="room within its cap", clause_ref=clause
        )
    admissible = money(line.claimed * cap / room_rate)
    reason = (
        f"proportionate deduction: room {_rupees(room_rate)} a day against a {_rupees(cap)} "
        f"cap, ratio {cap / room_rate * HUNDRED:.2f}%"
    )
    return RuleResult(
        rule_id="R13",
        passed=False,
        amount_effect=admissible - money(line.claimed),
        reason=reason,
        clause_ref=clause,
    )


def _daily_cap(rule_id: str, line: LineFacts, room: RoomFacts, product: ProductConfig
               ) -> RuleResult:  # fmt: skip
    """The room (R13) or ICU (R14) cap a day, times the days on the line."""
    limits = product.room_rent if line.head is Head.ROOM else product.icu
    clauses = product.clauses
    clause = clauses.room_cap if line.head is Head.ROOM else clauses.icu_cap
    cap = daily_cap(limits.percent_of_sum_insured, limits.max_per_day, room.sum_insured)
    days = line.qty if line.qty and line.qty > 0 else Decimal(1)
    rate = line.rate if line.rate is not None else line.claimed / days
    if rate <= cap:
        reason = f"{_rupees(rate)} a day is within the {_rupees(cap)} cap"
        return RuleResult(rule_id=rule_id, passed=True, reason=reason, clause_ref=clause)
    admissible = money(min(line.claimed, cap * days))
    reason = f"{_rupees(rate)} a day for {days} days exceeds the {_rupees(cap)} cap"
    return RuleResult(
        rule_id=rule_id,
        passed=False,
        amount_effect=admissible - money(line.claimed),
        reason=reason,
        clause_ref=clause,
    )


# --- R14: ICU cap -------------------------------------------------------------------------


def r14_icu_cap(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None or facts.room is None:
        return _missing("R14", *_absent(line=facts.line, room=facts.room))
    line = facts.line
    if line.head is not Head.ICU:
        return RuleResult(rule_id="R14", passed=True, reason=f"not an ICU line ({line.head})")
    if in_package(line.head, facts, product):
        return _priced_by_package("R14", facts)
    return _daily_cap("R14", line, facts.room, product)


# --- Packages: R20, R23, R24 --------------------------------------------------------------


def agreement_binds(facts: Facts, product: ProductConfig) -> bool:
    """True when the hospital agreement's pricing binds this claim's route."""
    return facts.claim_route in product.packages.pricing_applies_to


def _not_bound(rule_id: str, facts: Facts, product: ProductConfig) -> RuleResult | None:
    """The result of a package pricing rule that cannot apply: a missing-facts result when
    the claim route is unknown, a pass when the agreement does not bind the route. None when
    the rule applies."""
    if facts.claim_route is None:
        return _missing(rule_id, "claim_route")
    if agreement_binds(facts, product):
        return None
    reason = (
        f"the hospital agreement's package pricing does not bind a {facts.claim_route} claim "
        "under this wording; the policy's own limits apply"
    )
    if facts.package is not None and facts.package.packages:
        reason += f" (package {_codes(facts)} mapped)"
    return RuleResult(rule_id=rule_id, passed=True, reason=reason)


def in_package(head: Head, facts: Facts, product: ProductConfig) -> bool:
    """True when a package applies and prices this head: a package charge line, or a head the
    package covers, on a claim route the agreement binds."""
    return (
        facts.package is not None
        and agreement_binds(facts, product)
        and (head is Head.PACKAGE or head in product.packages.included_heads)
    )


def _codes(facts: Facts) -> str:
    assert facts.package is not None
    return ", ".join(p.package_code for p in facts.package.packages)


def _priced_by_package(rule_id: str, facts: Facts) -> RuleResult:
    return RuleResult(
        rule_id=rule_id, passed=True, reason=f"priced by the package ({_codes(facts)})"
    )


def package_shares(packages: list[MappedPackage], product: ProductConfig
                   ) -> list[tuple[MappedPackage, Decimal]]:  # fmt: skip
    """Each package with the percent of its rate paid. A stand-alone package is paid alone
    (the highest-rated one when there are several); add-ons are paid at the add-on percent;
    the rest at the first, second and later percents, highest rate first."""
    percents = product.packages.multi_procedure_percent
    standalone = [p for p in packages if p.standalone]
    if standalone and len(packages) > 1:
        kept = max(standalone, key=lambda p: p.rate)
        return [(p, HUNDRED if p is kept else ZERO) for p in packages]
    shares: list[tuple[MappedPackage, Decimal]] = [(p, percents.addon) for p in packages if p.addon]
    main = sorted((p for p in packages if not p.addon), key=lambda p: p.rate, reverse=True)
    for index, package in enumerate(main):
        percent = (percents.first, percents.second)[index] if index < 2 else percents.rest
        shares.append((package, percent))
    return shares


def r20_package_cap(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.package_tariff
    if facts.package is None or not facts.package.packages:
        return RuleResult(rule_id="R20", passed=True, reason="no package applies to the claim")
    if (unbound := _not_bound("R20", facts, product)) is not None:
        return unbound
    shares = package_shares(facts.package.packages, product)
    total = money(sum((p.rate * pct / HUNDRED for p, pct in shares), ZERO))
    parts = [f"{p.package_code} at {pct.normalize():f}% of {_rupees(p.rate)}" for p, pct in shares]
    if any(p.standalone for p in facts.package.packages) and len(shares) > 1:
        parts.append("a stand-alone package is paid alone")
    billed = money(facts.package.package_total)
    what = "package charges" if facts.package.billed_as_package else "charges the package covers"
    detail = f"{'; '.join(parts)}: package total {_rupees(total)}"
    if billed <= total:
        reason = f"{what} {_rupees(billed)} within the {detail}"
        return RuleResult(rule_id="R20", passed=True, reason=reason, clause_ref=clause)
    return RuleResult(
        rule_id="R20",
        passed=False,
        amount_effect=total - billed,
        reason=f"{what} {_rupees(billed)} capped at the agreed {detail}",
        clause_ref=clause,
    )


def r23_package_inclusions(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None:
        return _missing("R23", "line")
    line = facts.line
    package = facts.package
    if package is None or not package.billed_as_package:
        return RuleResult(rule_id="R23", passed=True, reason="no package charge on the bill")
    if (unbound := _not_bound("R23", facts, product)) is not None:
        return unbound
    if line.head not in product.packages.included_heads:
        reason = f"{line.head.value} is not inside the package"
        return RuleResult(rule_id="R23", passed=True, reason=reason)
    return RuleResult(
        rule_id="R23",
        passed=False,
        amount_effect=-money(line.claimed),
        reason=(
            f"{line.head.value} is already inside package {_codes(facts)}, which the bill "
            "charges separately"
        ),
        clause_ref=product.clauses.package_inclusions,
    )


def r24_non_package_discount(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None:
        return _missing("R24", "line")
    line = facts.line
    percent = facts.non_package_discount_percent
    if not percent:
        return RuleResult(rule_id="R24", passed=True, reason="no agreed non-package discount")
    if (unbound := _not_bound("R24", facts, product)) is not None:
        return unbound
    if in_package(line.head, facts, product):
        return _priced_by_package("R24", facts)
    if line.head in product.packages.discount_excluded_heads:
        reason = f"{line.head.value} is never discounted"
        return RuleResult(rule_id="R24", passed=True, reason=reason)
    discount = money(line.claimed * percent / HUNDRED)
    return RuleResult(
        rule_id="R24",
        passed=False,
        amount_effect=-discount,
        reason=f"agreed {percent.normalize():f}% discount on non-package charges",
        clause_ref=product.clauses.non_package_discount,
    )


# --- R12: the model's reading of an unclear line -------------------------------------------


def r12_unclear_line(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None or facts.judgement is None:
        return _missing("R12", *_absent(line=facts.line, judgement=facts.judgement))
    line, judgement = facts.line, facts.judgement
    item = judgement.item
    if judgement.confidence < product.non_payable_judging.min_confidence:
        read = "payable" if item is None else f"List {item.list_name} item {item.item_number}"
        return RuleResult(
            rule_id="R12",
            passed=False,
            reason=(
                f"model unsure ({judgement.confidence:.2f}) whether the line is {read}; "
                "a person decides"
            ),
            judged_by="model",
        )
    if item is None:
        reason = "read as payable: not a non-payable list item"
        return RuleResult(rule_id="R12", passed=True, reason=reason, judged_by="model")
    named = f"List {item.list_name} item {item.item_number} {item.item!r}"
    if item.payable_in_product in PAID_FLAGS:
        return RuleResult(rule_id="R12", passed=True, reason=f"paid: {named}", judged_by="model")
    return RuleResult(
        rule_id="R12",
        passed=False,
        amount_effect=-money(line.claimed),
        reason=f"non-payable: {named}",
        clause_ref=product.clauses.non_payable.get(item.list_name),
        judged_by="model",
    )


# --- R16, R17, R19: sub-limits -----------------------------------------------------------


def r16_cataract(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.cataract
    if product.sub_limits is None:
        return _missing("R16", "sub_limits")
    eyes = {
        code.strip().upper()[3]
        for code in facts.procedure_codes
        if has_prefix(code, product.cataract_pcs)
    }
    if not eyes:
        return RuleResult(rule_id="R16", passed=True, reason="no cataract surgery")
    if facts.sum_insured is None or facts.admissible_total is None:
        return _missing(
            "R16", *_absent(sum_insured=facts.sum_insured, admissible=facts.admissible_total)
        )
    limit = product.sub_limits.cataract
    per_eye = money(
        min(facts.sum_insured * limit.percent_of_sum_insured / HUNDRED, limit.max_per_eye)
    )
    cap = per_eye * len(eyes)
    total = money(facts.admissible_total)
    which = "both eyes" if len(eyes) == 2 else "one eye"
    if total <= cap:
        reason = f"{_rupees(total)} within the cataract limit of {_rupees(cap)} ({which})"
        return RuleResult(rule_id="R16", passed=True, reason=reason, clause_ref=clause)
    return RuleResult(
        rule_id="R16",
        passed=False,
        amount_effect=cap - total,
        reason=f"cataract limit {_rupees(per_eye)} an eye, {which}: {_rupees(total)} capped",
        clause_ref=clause,
    )


def r17_modern_treatments(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.modern_treatments
    found = [
        t.item
        for t in product.modern_treatments
        if any(has_prefix(code, t.icd10pcs) for code in facts.procedure_codes)
    ]
    if not found:
        return RuleResult(rule_id="R17", passed=True, reason="no modern treatment coded")
    if product.sub_limits is None or facts.sum_insured is None or facts.admissible_total is None:
        return _missing(
            "R17",
            *_absent(
                sub_limits=product.sub_limits,
                sum_insured=facts.sum_insured,
                admissible=facts.admissible_total,
            ),
        )
    percent = product.sub_limits.modern_treatments_percent_of_sum_insured
    cap = money(facts.sum_insured * percent / HUNDRED)
    total = money(facts.admissible_total)
    named = ", ".join(found)
    if total <= cap:
        reason = f"{named}: {_rupees(total)} within {percent}% of the sum insured"
        return RuleResult(rule_id="R17", passed=True, reason=reason, clause_ref=clause)
    return RuleResult(
        rule_id="R17",
        passed=False,
        amount_effect=cap - total,
        reason=f"{named}: capped at {percent}% of the sum insured, {_rupees(cap)}",
        clause_ref=clause,
    )


def r19_ambulance(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.ambulance
    if product.sub_limits is None or facts.ambulance_total is None:
        return _missing("R19", *_absent(sub_limits=product.sub_limits,
                                        ambulance=facts.ambulance_total))  # fmt: skip
    cap = money(product.sub_limits.ambulance_max_per_hospitalisation)
    total = money(facts.ambulance_total)
    if total <= cap:
        reason = f"ambulance {_rupees(total)} within the {_rupees(cap)} limit"
        return RuleResult(rule_id="R19", passed=True, reason=reason, clause_ref=clause)
    return RuleResult(
        rule_id="R19",
        passed=False,
        amount_effect=cap - total,
        reason=f"ambulance {_rupees(total)} capped at {_rupees(cap)} a hospitalisation",
        clause_ref=clause,
    )


# --- R18: pre and post hospitalisation windows -------------------------------------------


def r18_pre_post(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.pre_post
    if facts.line is None or facts.admission_date is None:
        return _missing("R18", *_absent(line=facts.line, admission_date=facts.admission_date))
    line = facts.line
    served = line.service_date
    if served is None:
        reason = "no service date on the line; not checked"
        return RuleResult(rule_id="R18", passed=True, reason=reason, clause_ref=clause)
    admitted = facts.admission_date
    discharged = facts.discharge_date or admitted
    pre, post = product.pre_hospitalisation_days, product.post_hospitalisation_days
    if pre is not None and served < admitted - timedelta(days=pre):
        reason = f"served {served}, more than {pre} days before admission on {admitted}"
    elif post is not None and served > discharged + timedelta(days=post):
        reason = f"served {served}, more than {post} days after discharge on {discharged}"
    else:
        reason = f"served {served}, within the pre and post hospitalisation windows"
        return RuleResult(rule_id="R18", passed=True, reason=reason, clause_ref=clause)
    return RuleResult(
        rule_id="R18",
        passed=False,
        amount_effect=-money(line.claimed),
        reason=reason,
        clause_ref=clause,
    )


# --- R26: final bill against the approved pre-auth ----------------------------------------


def r26_preauth(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.preauth
    preauth = facts.preauth
    if preauth is None:
        reason = "no pre-auth on the claim; nothing to compare"
        return RuleResult(rule_id="R26", passed=True, reason=reason, clause_ref=clause)
    if facts.admissible_total is None:
        return _missing("R26", "admissible_total")
    approved, total = money(preauth.approved_total), money(facts.admissible_total)
    approved_families = {c.strip().upper()[:3] for c in preauth.diagnosis_codes}
    verified_families = {c.strip().upper()[:3] for c in facts.diagnosis_codes}
    if total > approved:
        return RuleResult(
            rule_id="R26",
            passed=False,
            amount_effect=approved - total,
            reason=f"final bill {_rupees(total)} exceeds the approved {_rupees(approved)}",
            clause_ref=clause,
        )
    if approved_families and verified_families and not approved_families & verified_families:
        return RuleResult(
            rule_id="R26",
            passed=False,
            reason=(
                f"diagnosis changed since the pre-auth: approved "
                f"{', '.join(sorted(preauth.diagnosis_codes))}, verified "
                f"{', '.join(sorted(facts.diagnosis_codes))}"
            ),
            clause_ref=clause,
        )
    reason = f"final bill {_rupees(total)} within the approved {_rupees(approved)}"
    return RuleResult(rule_id="R26", passed=True, reason=reason, clause_ref=clause)


# --- R6, R7, R9: eligibility on verified codes -------------------------------------------


def r6_pre_existing(facts: Facts, product: ProductConfig) -> RuleResult:
    clause = product.clauses.pre_existing
    if facts.policy is None or facts.admission_date is None:
        return _missing("R6", *_absent(policy=facts.policy, admission_date=facts.admission_date))
    policy, admitted = facts.policy, facts.admission_date
    before = sorted(d for d in facts.pre_existing_onsets if d < policy.inception_date)
    declared = [c.code for c in policy.declared_peds]
    treated = [c for c in facts.diagnosis_codes if declared and code_in_ranges(c, declared)]
    if not before and not treated:
        reason = "no pre-existing disease found or declared for this treatment"
        return RuleResult(rule_id="R6", passed=True, reason=reason, clause_ref=clause)
    found = (
        f"declared pre-existing {', '.join(treated)}"
        if treated
        else f"a condition that started by {before[0]}, before inception {policy.inception_date}"
    )
    months = product.pre_existing_waiting_months
    if months is None:
        return _missing("R6", "pre_existing_waiting_months")
    served_from = add_months(policy.continuous_cover_start, months)
    passed = admitted >= served_from
    state = "served" if passed else "not served"
    reason = (
        f"{found}; the {months}-month wait from {policy.continuous_cover_start} is {state} "
        f"(ends {served_from})"
    )
    return RuleResult(rule_id="R6", passed=passed, reason=reason, clause_ref=clause)


def r7_specific_waiting(facts: Facts, product: ProductConfig) -> RuleResult:
    waiting = product.specific_waiting
    if waiting is None:
        return RuleResult(rule_id="R7", passed=True, reason="no specific waiting list")
    clause = waiting.clause
    if facts.policy is None or facts.admission_date is None:
        return _missing("R7", *_absent(policy=facts.policy, admission_date=facts.admission_date))
    if facts.accident and waiting.accident_exempt:
        reason = "hospitalisation due to an accident is exempt from the specific waits"
        return RuleResult(rule_id="R7", passed=True, reason=reason, clause_ref=clause)
    start = facts.policy.continuous_cover_start
    for condition in waiting.conditions:
        cm = [c for c in facts.diagnosis_codes if code_in_ranges(c, condition.icd10cm)]
        pcs = [c for c in facts.procedure_codes if has_prefix(c, condition.icd10pcs)]
        if not cm and not pcs:
            continue
        ends = add_months(start, condition.months)
        if facts.admission_date < ends:
            return RuleResult(
                rule_id="R7",
                passed=False,
                reason=(
                    f"{condition.item} ({', '.join([*cm, *pcs])}): {condition.months}-month "
                    f"wait from {start} ends {ends}, after admission on {facts.admission_date}"
                ),
                clause_ref=clause,
            )
    reason = "no listed condition is in its waiting period"
    return RuleResult(rule_id="R7", passed=True, reason=reason, clause_ref=clause)


def r9_exclusions(facts: Facts, product: ProductConfig) -> RuleResult:
    for exclusion in product.exclusions:
        cm = [c for c in facts.diagnosis_codes if code_in_ranges(c, exclusion.icd10cm)]
        pcs = [c for c in facts.procedure_codes if has_prefix(c, exclusion.icd10pcs)]
        if cm or pcs:
            return RuleResult(
                rule_id="R9",
                passed=False,
                reason=f"{exclusion.title} ({exclusion.code}): {', '.join([*cm, *pcs])}",
                clause_ref=exclusion.code,
            )
    return RuleResult(rule_id="R9", passed=True, reason="no excluded condition in the codes")


# --- Drug price check: a finding, never a deduction ---------------------------------------

_COUNTED = {"vial", "tablet", "capsule", "ampoule", "amp", "pack", "bottle", "sachet", "tube"}
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|g|gm|mcg|ml)\b", re.IGNORECASE)
_PER_ML = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg)\s*/\s*(\d+(?:\.\d+)?)?\s*ml\b",
                     re.IGNORECASE)  # fmt: skip


def _amounts(text: str) -> dict[str, Decimal]:
    """The first mg (grams converted) and mL amounts written in the text."""
    found: dict[str, Decimal] = {}
    for number, unit in _STRENGTH.findall(text):
        unit = unit.lower()
        value = Decimal(number)
        if unit in ("g", "gm"):
            value, unit = value * 1000, "mg"
        found.setdefault(unit, value)
    return found


def _units_billed(line: LineFacts, drug: DrugFacts) -> Decimal | None:
    """How many of the ceiling's units one billed unit holds, or None when the units cannot
    be compared."""
    unit = drug.unit.lower().split()
    if len(unit) == 2 and unit[0] == "1" and unit[1] in _COUNTED:
        return Decimal(1)
    if unit != ["1", "ml"]:
        return None
    written = _amounts(line.description)
    if "ml" in written:
        return written["ml"]
    concentration = _PER_ML.search(drug.dosage_form)
    if concentration and "mg" in written:
        mg = Decimal(concentration.group(1))
        per_ml = Decimal(concentration.group(3) or 1)
        return written["mg"] * per_ml / mg
    return None


def drug_price(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None or facts.drug is None:
        return _missing("DRUG-PRICE", *_absent(line=facts.line, drug=facts.drug))
    line, drug = facts.line, facts.drug
    units = _units_billed(line, drug)
    qty = line.qty if line.qty and line.qty > 0 else Decimal(1)
    unit_price = line.rate if line.rate is not None else line.claimed / qty
    gst = product.drug_price.gst_percent
    if units is None:
        reason = (
            f"cannot compare {line.description!r} with the NPPA ceiling per {drug.unit} "
            f"({drug.dosage_form}); GST not applied"
        )
        return RuleResult(rule_id="DRUG-PRICE", passed=True, reason=reason)
    ceiling = money(drug.ceiling_price * units * (HUNDRED + gst) / HUNDRED)
    basis = (
        f"NPPA ceiling {drug.ceiling_price} per {drug.unit} ({drug.medicine} {drug.dosage_form})"
        f" x {units.normalize():f}, plus {gst.normalize():f}% GST = {_rupees(ceiling)}"
    )
    over = money(unit_price) > ceiling
    state = "above" if over else "within"
    reason = f"{_rupees(unit_price)} a unit is {state} the {basis}"
    return RuleResult(rule_id="DRUG-PRICE", passed=not over, reason=reason)


# --- Spreading a claim-level amount over lines ------------------------------------------


def allocate(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    total = sum((w for w in weights if w > 0), ZERO)
    if total <= 0:
        return [ZERO for _ in weights]
    last = max(i for i, w in enumerate(weights) if w > 0)
    shares = [money(amount * w / total) if w > 0 else ZERO for w in weights]
    shares[last] = money(amount - sum((s for i, s in enumerate(shares) if i != last), ZERO))
    return shares


# --- R21: co-pay, applied last ------------------------------------------------------------


def r21_co_pay(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.admissible_total is None:
        return _missing("R21", "admissible_total")
    percent = product.co_pay.percent
    copay = money(facts.admissible_total * percent / HUNDRED)
    return RuleResult(
        rule_id="R21",
        passed=True,
        amount_effect=-copay,
        reason=f"{percent}% co-pay on {_rupees(facts.admissible_total)}",
        clause_ref=product.clauses.co_pay,
    )


RULES: dict[str, Rule] = {
    "R1": r1_premium_received,
    "R2": r2_in_force,
    "R3": r3_member_match,
    "R5": r5_initial_wait,
    "R6": r6_pre_existing,
    "R7": r7_specific_waiting,
    "R9": r9_exclusions,
    "R10": r10_sum_insured,
    "R11": r11_non_payable,
    "R12": r12_unclear_line,
    "R13": r13_room_cap,
    "R14": r14_icu_cap,
    "R16": r16_cataract,
    "R17": r17_modern_treatments,
    "R18": r18_pre_post,
    "R19": r19_ambulance,
    "R20": r20_package_cap,
    "R21": r21_co_pay,
    "R23": r23_package_inclusions,
    "R24": r24_non_package_discount,
    "R26": r26_preauth,
    "DRUG-PRICE": drug_price,
}
