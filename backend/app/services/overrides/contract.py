"""Contract for the override log: every change a person makes to a machine suggestion.

The services that take a person's correction write the log as they store the corrected
record: label fixes (classification), field fixes (digitisation), registration overrides,
deduction edits (bill review), medical overrules and decision overrides. Each entry says who,
when, which field, the value before and after, and the reason given.

Callers import this module only.
"""

from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.types import Stage

OverrideKind = Literal[
    "label_fix",
    "field_fix",
    "registration_override",
    "deduction_edit",
    "medical_overrule",
    "decision_override",
]


class OverrideEntry(BaseModel):
    claim_id: UUID
    stage: Stage
    kind: OverrideKind
    field: str  # what changed, e.g. "identity.patient_name", "line 7 admissible", "R10"
    before: Any  # the machine's value (JSON)
    after: Any  # the person's value (JSON)
    reason: str  # empty when the checkpoint asks for none (label and field fixes)
    actor: str  # "user:<id>" when the person is known, else the actor the stage recorded


class Override(OverrideEntry):
    override_id: UUID
    user_id: UUID | None  # parsed from actor "user:<id>"
    claim_number: str | None = None
    created_at: datetime


class OverrideLog(Protocol):
    async def list(
        self, *, claim_id: UUID | None = None, user_id: UUID | None = None, limit: int = 200
    ) -> list[Override]:
        """Newest first."""
        ...
