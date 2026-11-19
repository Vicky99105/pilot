"""Contract for every model call. Callers import this module only; no service imports an
LLM SDK."""

from enum import StrEnum
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError

T = TypeVar("T", bound=BaseModel)


class ModelRole(StrEnum):
    VISION_CLASSIFY = "vision_classify"
    VISION_EXTRACT = "vision_extract"
    REASONING = "reasoning"
    FAST_TEXT = "fast_text"


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelOutputInvalid(PermanentError):
    """The model's output failed schema validation twice."""


class ModelUnavailable(Exception):
    """The provider failed in a way that a later retry can fix."""


class ModelGateway(Protocol):
    async def structured(
        self,
        role: ModelRole,
        messages: list[Message],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T: ...

    async def vision(
        self,
        role: ModelRole,
        prompt: str,
        images: list[bytes],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T: ...
