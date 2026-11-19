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
    """The provider failed in a way that a later retry can fix. retry_after_seconds is the
    wait the provider asked for (a 429's retryDelay or Retry-After), when it named one."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ModelQuotaExhausted(PermanentError):
    """The provider's quota for the day is used up. Retrying within the day cannot succeed."""


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
