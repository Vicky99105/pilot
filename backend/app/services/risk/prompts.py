"""Prompt and reply schema for the risk explanation. The model explains; it never scores."""

import json
from typing import Any

from pydantic import BaseModel

from app.services.model_gateway.contract import Message

PURPOSE = "risk_explanation"


class RiskExplanation(BaseModel):
    text: str


def risk_messages(hits: list[dict[str, Any]], score: int, refer: bool) -> list[Message]:
    return [
        Message(
            role="system",
            content=(
                "You explain a claim's fraud red flags to an investigator in two or three plain "
                "sentences. The score and the referral are already decided; do not change them "
                "or present them as your own judgement, and do not compute anything."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Red flags:\n{json.dumps(hits, indent=1)}\nScore: {score}/100. Referred: {refer}."
            ),
        ),
    ]
