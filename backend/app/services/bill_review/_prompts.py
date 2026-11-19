"""The two model calls of bill review: the package choice and the reading of unclear lines
(R12). Each reply schema is built for the call, with enum lists of the only values allowed.
Private to the bill review service."""

import json
from typing import Any

from pydantic import BaseModel, Field

from app.services.bill_review.contract import NO_PACKAGE, PackageOption
from app.services.knowledge_base.contract import NonPayableCandidate
from app.services.model_gateway.contract import Message
from app.shared.closed_schema import closed_schema, one_of

PACKAGE_PURPOSE = "bill_package_choice"
NON_PAYABLE_PURPOSE = "bill_non_payable"
PAYABLE = "payable"


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


# --- Package choice ---------------------------------------------------------------------


class PackageChoiceReply(BaseModel):
    """One field per procedure, procedure_1 to procedure_n, each a package code or none."""

    model_config = {"extra": "forbid"}


def procedure_field(index: int) -> str:
    return f"procedure_{index}"


def package_schema(procedures: list[str], candidates: list[PackageOption]
                   ) -> type[PackageChoiceReply]:  # fmt: skip
    """Each procedure's field is an enum of the candidate package codes and NO_PACKAGE."""
    allowed = one_of([*dict.fromkeys(c.package_code for c in candidates), NO_PACKAGE])
    fields = {procedure_field(i): (allowed, ...) for i in range(1, len(procedures) + 1)}
    return closed_schema(PackageChoiceReply, "PackageChoiceReply", **fields)


def package_messages(
    procedures: list[str], diagnoses: list[str], candidates: list[PackageOption]
) -> list[Message]:
    view = {
        "procedures": {procedure_field(i): p for i, p in enumerate(procedures, start=1)},
        "diagnoses": diagnoses,
        "packages": [
            {"code": c.package_code, "name": c.name, "scheme": c.scheme} for c in candidates
        ],
    }
    return [
        Message(
            role="system",
            content=(
                "You map each procedure performed to the package of the hospital's agreed "
                "package list that pays for it. Answer with a package code for each "
                f"procedure, or {NO_PACKAGE!r} when no package listed is that procedure."
            ),
        ),
        Message(role="user", content=_json(view)),
    ]


# --- R12: unclear lines -----------------------------------------------------------------


class LineReading(BaseModel):
    choice: str
    confidence: float = Field(ge=0, le=1)


class LineReadingsReply(BaseModel):
    """One field per unclear line, line_1 to line_n."""

    model_config = {"extra": "forbid"}


def line_field(index: int) -> str:
    return f"line_{index}"


def item_label(candidate: NonPayableCandidate) -> str:
    return f"List {candidate.list_name} item {candidate.item_number}: {candidate.item}"


def readings_schema(candidates: list[list[NonPayableCandidate]]) -> type[LineReadingsReply]:
    """Each line's choice is an enum of that line's own candidate items and PAYABLE."""
    fields: dict[str, Any] = {}
    for i, found in enumerate(candidates, start=1):
        reading = closed_schema(
            LineReading,
            f"LineReading{i}",
            choice=(one_of([*(item_label(c) for c in found), PAYABLE]), ...),
        )
        fields[line_field(i)] = (reading, ...)
    return closed_schema(LineReadingsReply, "LineReadingsReply", **fields)


def readings_messages(
    lines: list[tuple[str, str]], candidates: list[list[NonPayableCandidate]]
) -> list[Message]:
    """lines: (description, head) for each unclear line."""
    view = {
        line_field(i): {
            "description": description,
            "head": head,
            "candidates": [{"item": item_label(c), "condition": c.condition} for c in found],
        }
        for i, ((description, head), found) in enumerate(
            zip(lines, candidates, strict=True), start=1
        )
    }
    return [
        Message(
            role="system",
            content=(
                "You review hospital bill lines for an Indian health insurer. For each line, "
                "choose the non-payable list item the line is, or "
                f"{PAYABLE!r} when it is none of them, and give your confidence from 0 to 1."
            ),
        ),
        Message(role="user", content=_json(view)),
    ]
