"""Payee checks. Private to settlement."""

import re

from rapidfuzz import fuzz

from app.services.digitisation.contract import Bank
from app.services.intake.contract import ClaimForm
from app.services.settlement.contract import HOLDER_MATCH_SCORE, IFSC_PATTERN, PayeeCheck

TITLES = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt"}


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def normalise_name(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in TITLES)


def payee_checks(
    cheque: Bank, claim_form: Bank, member_name: str | None, submit_form: ClaimForm | None = None
) -> list[PayeeCheck]:
    ifsc = (cheque.ifsc.value or "").strip().upper()
    holder = cheque.account_holder.value or ""
    score = (
        fuzz.token_sort_ratio(normalise_name(holder), normalise_name(member_name))
        if holder and member_name
        else 0.0
    )
    account, form_account = (
        digits(cheque.account_number.value),
        digits(claim_form.account_number.value),
    )
    form_ifsc = (claim_form.ifsc.value or "").strip().upper()
    same = bool(account) and account == form_account and ifsc == form_ifsc
    third = []
    if submit_form is not None and (submit_form.bank_account_number or submit_form.ifsc):
        typed_account = digits(submit_form.bank_account_number)
        typed_ifsc = (submit_form.ifsc or "").strip().upper()
        third.append(
            PayeeCheck(
                check_id="cheque_matches_submit_form",
                passed=bool(account) and account == typed_account and ifsc == typed_ifsc,
                detail=(
                    f"cheque account {account or '(none)'} / {ifsc or '(none)'} against the "
                    f"submitted form {typed_account or '(none)'} / {typed_ifsc or '(none)'}"
                ),
            )
        )
    return [
        PayeeCheck(
            check_id="ifsc_format",
            passed=re.fullmatch(IFSC_PATTERN, ifsc) is not None,
            detail=f"IFSC {ifsc or '(none)'} against {IFSC_PATTERN}",
        ),
        PayeeCheck(
            check_id="holder_matches_member",
            passed=score >= HOLDER_MATCH_SCORE,
            detail=(
                f"account holder {holder or '(none)'} against member "
                f"{member_name or '(unknown)'}: score {score:.0f} of {HOLDER_MATCH_SCORE} needed"
            ),
        ),
        PayeeCheck(
            check_id="cheque_matches_claim_form",
            passed=same,
            detail=(
                f"cheque account {account or '(none)'} / {ifsc or '(none)'} against claim form "
                f"Part A {form_account or '(none)'} / {form_ifsc or '(none)'}"
            ),
        ),
        *third,
    ]
