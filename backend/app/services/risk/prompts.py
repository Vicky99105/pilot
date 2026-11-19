"""Prompt and reply schema for the risk explanation. The model explains the hits it is
given; code has already scored them."""

import json

from pydantic import BaseModel

from app.services.model_gateway.contract import Message
from app.services.risk.contract import ReferReason, RiskHit

PURPOSE = "risk_explanation"

REFERRALS = {
    "score": "the score reached the referral threshold",
    "value": "the claimed amount reached the value threshold for investigation",
}


class RiskExplanation(BaseModel):
    text: str


def risk_messages(hits: list[RiskHit], score: int, reasons: list[ReferReason]) -> list[Message]:
    flagged = [{"flag": h.rule_id, "title": h.title, "detail": h.detail} for h in hits]
    referral = "; ".join(REFERRALS[r] for r in reasons) or "not referred"
    return [
        Message(
            role="system",
            content=(
                "Explain a claim's fraud red flags to an investigator in two or three plain "
                "sentences, from the flags listed."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Red flags:\n{json.dumps(flagged, indent=1)}\n"
                f"Score: {score}/100. Referral: {referral}."
            ),
        ),
    ]
