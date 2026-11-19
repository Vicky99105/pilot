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
    Facts,
    Head,
    MemberMatch,
    NonPayableItem,
    ProductConfig,
)
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
    if item.payable_in_product in PAID_FLAGS:
        return RuleResult(
            rule_id="R11",
            passed=True,
            reason=f"paid: List {item.list_name} item {item.item_number} {item.item!r}{condition}",
        )
    return RuleResult(
        rule_id="R11",
        passed=False,
        amount_effect=-money(line.claimed),
        reason=(
            f"non-payable: List {item.list_name} item {item.item_number} {item.item!r}{condition}"
        ),
        clause_ref=product.clauses.non_payable.get(item.list_name),
    )


# --- R13: room and ICU caps with proportionate deduction ----------------------------------


def daily_cap(percent: Decimal, maximum: Decimal, sum_insured: Decimal) -> Decimal:
    return money(min(sum_insured * percent / HUNDRED, maximum))


def r13_room_cap(facts: Facts, product: ProductConfig) -> RuleResult:
    if facts.line is None or facts.room is None:
        return _missing("R13", *_absent(line=facts.line, room=facts.room))
    line, room = facts.line, facts.room
    clauses = product.clauses
    if line.head in (Head.ROOM, Head.ICU):
        limits = product.room_rent if line.head is Head.ROOM else product.icu
        clause = clauses.room_cap if line.head is Head.ROOM else clauses.icu_cap
        cap = daily_cap(limits.percent_of_sum_insured, limits.max_per_day, room.sum_insured)
        days = line.qty if line.qty and line.qty > 0 else Decimal(1)
        rate = line.rate if line.rate is not None else line.claimed / days
        if rate <= cap:
            reason = f"{_rupees(rate)} a day is within the {_rupees(cap)} cap"
            return RuleResult(rule_id="R13", passed=True, reason=reason, clause_ref=clause)
        admissible = money(min(line.claimed, cap * days))
        reason = f"{_rupees(rate)} a day for {days} days exceeds the {_rupees(cap)} cap"
        return RuleResult(
            rule_id="R13",
            passed=False,
            amount_effect=admissible - money(line.claimed),
            reason=reason,
            clause_ref=clause,
        )
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
    "R10": r10_sum_insured,
    "R11": r11_non_payable,
    "R13": r13_room_cap,
    "R21": r21_co_pay,
}
