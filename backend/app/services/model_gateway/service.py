"""Default ModelGateway: role-to-model mapping from config/models.yaml, JSON-schema output,
one retry on a validation failure, and a model_calls row for every attempt."""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ModelCallRow
from app.integrations.openai_compatible import (
    ChatResponse,
    OpenAICompatibleProvider,
    ProviderQuotaExhausted,
    ProviderTransientError,
)
from app.services.file_store.contract import FileStore
from app.services.model_gateway._helpers import data_url, retry_instruction
from app.services.model_gateway.contract import (
    Message,
    ModelOutputInvalid,
    ModelQuotaExhausted,
    ModelRole,
    ModelUnavailable,
    T,
)

MAX_ATTEMPTS = 2  # the first call plus one retry on a validation failure


class ModelPricing(BaseModel):
    """USD per million tokens, used when the provider does not report a cost itself."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


class ProviderConfig(BaseModel):
    kind: Literal["openai_compatible"]
    base_url: str
    api_key_env: str
    request_extras: dict[str, Any] = {}
    pricing: dict[str, ModelPricing] = {}


class RoleConfig(BaseModel):
    provider: str
    model: str
    params: dict[str, Any] = {}


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    roles: dict[ModelRole, RoleConfig]


def load_models_config(path: Path) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text()))


class DefaultModelGateway:
    def __init__(
        self,
        *,
        config: ModelsConfig,
        providers: Mapping[str, OpenAICompatibleProvider],
        file_store: FileStore,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._providers = providers
        self._file_store = file_store
        self._sessions = sessions

    async def structured(
        self,
        role: ModelRole,
        messages: list[Message],
        schema: type[T],
        *,
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        wire = [m.model_dump() for m in messages]
        return await self._complete(role, wire, wire, [], schema, claim_id, purpose)

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
        keys = [await self._file_store.put(img, content_type="image") for img in images]
        text_part = {"type": "text", "text": prompt}
        wire = [
            {
                "role": "user",
                "content": [
                    text_part,
                    *({"type": "image_url", "image_url": {"url": data_url(i)}} for i in images),
                ],
            }
        ]
        logged = [
            {
                "role": "user",
                "content": [text_part, *({"type": "image_ref", "key": k} for k in keys)],
            }
        ]
        return await self._complete(role, wire, logged, keys, schema, claim_id, purpose)

    async def _complete(
        self,
        role: ModelRole,
        wire: list[dict[str, Any]],
        logged: list[dict[str, Any]],
        image_keys: list[str],
        schema: type[T],
        claim_id: UUID | None,
        purpose: str,
    ) -> T:
        role_config = self._config.roles[role]
        provider = self._providers[role_config.provider]
        json_schema = schema.model_json_schema()
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            log = _CallLog(claim_id, purpose, role, provider.name, role_config.model, attempt)
            started = time.monotonic()
            try:
                reply = await provider.chat_json(
                    model=role_config.model,
                    messages=wire,
                    schema_name=schema.__name__,
                    json_schema=json_schema,
                    params=role_config.params,
                )
            except Exception as exc:
                await self._log(
                    log, logged, image_keys, None, started, f"{type(exc).__name__}: {exc}"
                )
                if isinstance(exc, ProviderTransientError):
                    raise ModelUnavailable(
                        str(exc), retry_after_seconds=exc.retry_after_seconds
                    ) from exc
                if isinstance(exc, ProviderQuotaExhausted):
                    raise ModelQuotaExhausted(f"{purpose}: {exc}") from exc
                raise
            try:
                result = schema.model_validate_json(reply.content)
            except ValidationError as exc:
                last_error = str(exc)
                await self._log(log, logged, image_keys, reply, started, last_error)
                correction = [
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": retry_instruction(last_error)},
                ]
                wire, logged = [*wire, *correction], [*logged, *correction]
                continue
            await self._log(log, logged, image_keys, reply, started, None)
            return result
        raise ModelOutputInvalid(f"{purpose}: output failed {schema.__name__} twice: {last_error}")

    async def _log(
        self,
        log: "_CallLog",
        prompt: list[dict[str, Any]],
        image_keys: list[str],
        reply: ChatResponse | None,
        started: float,
        error: str | None,
    ) -> None:
        row = ModelCallRow(
            id=uuid4(),
            claim_id=log.claim_id,
            purpose=log.purpose,
            role=log.role.value,
            provider=log.provider,
            model=reply.model if reply else log.model,
            attempt=log.attempt,
            prompt=prompt,
            image_keys=image_keys,
            output=reply.content if reply else None,
            valid=error is None,
            error=error,
            input_tokens=reply.input_tokens if reply else None,
            output_tokens=reply.output_tokens if reply else None,
            cost=self._cost(log, reply),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        async with self._sessions.begin() as session:
            session.add(row)

    def _cost(self, log: "_CallLog", reply: ChatResponse | None) -> Decimal | None:
        if reply is None:
            return None
        if reply.cost is not None:
            return reply.cost
        pricing = self._config.providers[log.provider].pricing.get(log.model)
        if pricing is None or reply.input_tokens is None or reply.output_tokens is None:
            return None
        million = Decimal(1_000_000)
        return (
            reply.input_tokens * pricing.input_per_mtok
            + reply.output_tokens * pricing.output_per_mtok
        ) / million


@dataclass(frozen=True)
class _CallLog:
    claim_id: UUID | None
    purpose: str
    role: ModelRole
    provider: str
    model: str
    attempt: int


def build_model_gateway(
    *,
    config_path: Path,
    file_store: FileStore,
    sessions: async_sessionmaker[AsyncSession],
) -> DefaultModelGateway:
    """Build the gateway with one provider client per configured provider.

    API keys are read from the environment variable each provider names. A missing key
    does not stop startup; calls through that provider fail with a permanent error.
    """
    config = load_models_config(config_path)
    providers = {
        name: OpenAICompatibleProvider(
            name=name,
            base_url=p.base_url,
            api_key=SecretStr(os.environ.get(p.api_key_env, "")),
            extra_body=p.request_extras,
        )
        for name, p in config.providers.items()
    }
    return DefaultModelGateway(
        config=config, providers=providers, file_store=file_store, sessions=sessions
    )
