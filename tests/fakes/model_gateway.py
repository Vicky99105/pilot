from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.services.model_gateway.contract import Message, ModelRole, T


@dataclass
class RecordedCall:
    role: ModelRole
    purpose: str
    claim_id: UUID | None
    messages: list[Message] | None = None
    prompt: str | None = None
    image_count: int = 0


class FakeModelGateway:
    """Implements the ModelGateway contract. Replies come from a script keyed by purpose,
    consumed in order; each reply is a dict or model validated against the call's schema."""

    def __init__(self, script: dict[str, list[dict[str, Any] | BaseModel]]) -> None:
        self._script = {purpose: list(replies) for purpose, replies in script.items()}
        self.calls: list[RecordedCall] = []

    async def structured(
        self,
        role: ModelRole,
        messages: list[Message],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        self.calls.append(RecordedCall(role, purpose, claim_id, messages=messages))
        return self._next(purpose, schema)

    async def vision(
        self,
        role: ModelRole,
        prompt: str,
        images: list[bytes],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        self.calls.append(
            RecordedCall(role, purpose, claim_id, prompt=prompt, image_count=len(images))
        )
        return self._next(purpose, schema)

    def _next(self, purpose: str, schema: type[T]) -> T:
        replies = self._script.get(purpose)
        if not replies:
            raise AssertionError(f"FakeModelGateway: no scripted reply left for {purpose!r}")
        reply = replies.pop(0)
        data = reply.model_dump() if isinstance(reply, BaseModel) else reply
        return schema.model_validate(data)
