"""The red flags that need no other claim, the outlier score and the room rate. Pure."""

from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.integrations.core.contract import MemberPolicy
from app.services.bill_review.contract import DeductionSheet
from app.services.digitisation.contract import ClaimRecord
from app.services.medical_review.contract import MedicalFindings
from app.services.registration.contract import RegistrationResult
from app.services.risk.contract import OutlierScore, RiskConfig, VolumeJump
from app.services.rules.contract import Head

Hit = tuple[str, str]
TEN = Decimal(10)


def _rupees(value: Decimal) -> str:
    return f"Rs {value:,.2f}"


def policy_timing(
    registration: RegistrationResult, policy: MemberPolicy | None, config: RiskConfig
) -> list[Hit]:
    """RF1 soon after inception, RF4 close to the policy's end."""
    admitted = registration.admission_date
    if policy is None or admitted is None:
        return []
    found = []
    since = (admitted - policy.inception_date).days
    if 0 <= since < config.near_start_days:
        found.append(
            ("RF1", f"admitted {since} days after policy inception on {policy.inception_date}")
        )
    left = (policy.policy_end - admitted).days
    if 0 <= left < config.near_expiry_days:
        found.append(("RF4", f"admitted {left} days before the policy ends on {policy.policy_end}"))
    return found


def high_claim(
    claimed: Decimal | None, registration: RegistrationResult, config: RiskConfig
) -> list[Hit]:
    """RF2: the claimed amount over a share of the sum insured."""
    if claimed is None or registration.policy is None:
        return []
    sum_insured = registration.policy.sum_insured
    if claimed <= sum_insured * config.high_claim_share:
        return []
    share = f"{config.high_claim_share * 100:.0f}%"
    return [
        ("RF2", f"claimed {_rupees(claimed)} is over {share} of the sum insured "
                f"{_rupees(sum_insured)}")
    ]  # fmt: skip


def primary_category(coding: dict[str, Any] | None) -> str | None:
    """The 3-character category of a MedicalCoding payload's first verified code."""
    codes = (coding or {}).get("verified_codes") or []
    if not codes:
        return None
    code = str(codes[0]["code"]["code"]).strip().upper()
    return code[:3] or None


def doctor(record: ClaimRecord | None) -> str | None:
    """The treating doctor's registration number, as written, when the claim has one."""
    if record is None:
        return None
    number = record.preauth.doctor_registration_number.value
    return number.strip().upper() if number and number.strip() else None


def volume_jump(recent: int, baseline: int, rules: VolumeJump) -> str | None:
    """A description of the jump, or None: at least min_recent claims in the recent window,
    and at least ratio times the baseline's average per window."""
    windows = Decimal(rules.baseline_days) / Decimal(rules.recent_days)
    average = Decimal(baseline) / windows
    if recent < rules.min_recent or Decimal(recent) < rules.ratio * average:
        return None
    return (
        f"{recent} claims in the last {rules.recent_days} days against an average of "
        f"{average:.1f} per {rules.recent_days} days over the {rules.baseline_days} days before"
    )


def room_rate(record: ClaimRecord | None, sheet: DeductionSheet | None) -> Decimal | None:
    """The room rate charged a day: the sheet's room lines over the days the bill gives them.
    None without a final bill (an estimate has no days per line) or a room line."""
    if record is None or sheet is None or not record.bill.lines:
        return None
    days = {ln.line_id: ln.qty.value for ln in record.bill.lines}
    room = [ln for ln in sheet.lines if ln.head is Head.ROOM]
    total_days = sum((days.get(ln.line_id) or Decimal(1) for ln in room), Decimal(0))
    if not room or total_days <= 0:
        return None
    rate = sum((ln.claimed for ln in room), Decimal(0)) / total_days
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def bill_form(record: ClaimRecord | None) -> list[Hit]:
    """RF9: the final bill read as not on letterhead, or with handwritten values."""
    if record is None or not record.bill.lines:
        return []
    found = []
    if record.bill.on_letterhead.value is False:
        found.append("the final bill is not on the hospital's letterhead")
    written = [ln.line_id for ln in record.bill.lines if ln.amount.handwritten]
    if record.bill.grand_total.handwritten:
        found.append("the bill's grand total is handwritten")
    if written:
        found.append(f"handwritten amounts on bill lines {', '.join(map(str, written))}")
    return [("RF9", "; ".join(found))] if found else []


def _gender(text: str | None) -> str | None:
    initial = (text or "").strip()[:1].upper()
    return initial if initial in ("M", "F") else None


def identity(record: ClaimRecord | None, registration: RegistrationResult) -> list[Hit]:
    """RF10: the claim's member ID, date of birth or gender against the matched member."""
    member = registration.member
    if record is None or member.member_id is None or registration.policy is None:
        return []
    insured = next(
        (m for m in registration.policy.insured_members if m.member_id == member.member_id), None
    )
    if insured is None:
        return []
    found = []
    written_id = record.identity.member_id.value
    if written_id and _ident(written_id) != _ident(insured.member_id):
        found.append(f"member ID {written_id} on the claim, {insured.member_id} on the policy")
    born: date | None = record.identity.date_of_birth.value
    if born is not None and born != insured.dob:
        found.append(f"date of birth {born} on the claim, {insured.dob} on the policy")
    gender = _gender(record.identity.gender.value)
    if gender is not None and gender != insured.gender:
        found.append(f"gender {record.identity.gender.value} on the claim, {insured.gender} "
                     "on the policy")  # fmt: skip
    return [("RF10", "; ".join(found))] if found else []


def _ident(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def outlier(
    record: ClaimRecord | None,
    sheet: DeductionSheet | None,
    findings: MedicalFindings | None,
    claimed: Decimal | None,
    config: RiskConfig,
) -> tuple[OutlierScore | None, list[Hit]]:
    """The bill against the mapped package rate (RF11) and the stay against the norm (RF12),
    and the outlier score from both. None when neither baseline applies."""
    rules = config.outlier
    rate = None
    if sheet is not None and sheet.package is not None and sheet.package.packages:
        rate = sum((p.rate for p in sheet.package.packages), Decimal(0))
    billed = sheet.claimed_total if sheet is not None and sheet.lines else claimed
    ratio = None
    if rate and billed is not None:
        ratio = (billed / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    norm = findings.guideline.los_norm_days if findings and findings.guideline else None
    stay = _stay_days(record)
    if ratio is None and (norm is None or stay is None):
        return None, []
    hits: list[Hit] = []
    points = 0
    if ratio is not None and rate is not None and billed is not None:
        tenths = ((billed / rate - 1) * TEN).to_integral_value(rounding=ROUND_DOWN)
        points += max(0, int(tenths)) * rules.tariff_points_per_10_percent
        if ratio >= rules.tariff_ratio:
            hits.append(("RF11", f"bill {_rupees(billed)} is {ratio} times the package rate "
                                 f"{_rupees(rate)}"))  # fmt: skip
    if norm is not None and stay is not None:
        extra = stay - norm
        points += max(0, extra) * rules.los_points_per_day
        if extra >= rules.los_extra_days:
            hits.append(("RF12", f"{stay}-day stay against a {norm}-day norm"))
    score = OutlierScore(
        score=min(100, points),
        tariff_ratio=ratio,
        package_rate=rate.quantize(Decimal("0.01")) if rate is not None else None,
        stay_days=stay,
        los_norm_days=norm,
    )
    return score, hits


def _stay_days(record: ClaimRecord | None) -> int | None:
    if record is None:
        return None
    admitted = record.admission.admission_date.value
    discharged = record.admission.discharge_date.value
    if admitted is None or discharged is None or discharged < admitted:
        return None
    return (discharged - admitted).days
