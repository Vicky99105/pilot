"""Client for any OpenAI-compatible chat completions API (OpenRouter, vLLM, Azure and others)."""

from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

from app.shared.errors import PermanentError


class ProviderTransientError(Exception):
    """Timeout, rate limit or server error. Retrying later can succeed."""


class ProviderPermanentError(PermanentError):
    """Bad request, auth failure or missing key. Retrying cannot succeed."""


class ChatResponse(BaseModel):
    content: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None  # in the provider's billing currency, when it reports one


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: SecretStr,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)
        # Provider-specific request fields, for example OpenRouter's cost reporting.
        self._extra_body = dict(extra_body or {})

    async def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        schema_name: str,
        json_schema: dict[str, Any],
        params: dict[str, Any],
    ) -> ChatResponse:
        key = self._api_key.get_secret_value()
        if not key:
            raise ProviderPermanentError(f"no API key configured for provider {self.name}")
        body = {
            **self._extra_body,
            **params,
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": False},
            },
        }
        try:
            response = await self._http.post(
                self._url, json=body, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderTransientError(f"HTTP {response.status_code}: {response.text[:500]}")
        if response.status_code >= 400:
            raise ProviderPermanentError(f"HTTP {response.status_code}: {response.text[:500]}")
        return _parse(response.json(), model)


def _parse(data: dict[str, Any], requested_model: str) -> ChatResponse:
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderTransientError(f"malformed completion: {str(data)[:500]}") from exc
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    return ChatResponse(
        content=content,
        model=data.get("model") or requested_model,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost=Decimal(str(cost)) if cost is not None else None,
    )
