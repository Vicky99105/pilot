"""Client for any OpenAI-compatible chat completions API (OpenRouter, vLLM, Azure and others)."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

from app.shared.errors import PermanentError


class ProviderTransientError(Exception):
    """Timeout, rate limit or server error. Retrying later can succeed."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderPermanentError(PermanentError):
    """Bad request, auth failure or missing key. Retrying cannot succeed."""


class ProviderQuotaExhausted(ProviderPermanentError):
    """A 429 for a quota counted per day. Retrying within the day cannot succeed."""


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
        if response.status_code == 429 and _per_day_quota(response):
            raise ProviderQuotaExhausted(
                f"{self.name}: the quota per day is used up: {response.text[:500]}"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderTransientError(
                f"HTTP {response.status_code}: {response.text[:500]}",
                retry_after_seconds=_retry_after(response),
            )
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
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    # Some providers (Gemini) bill thinking tokens as output but leave them out of
    # completion_tokens; the total still includes them.
    if input_tokens is not None and total is not None and output_tokens is not None:
        output_tokens = max(output_tokens, total - input_tokens)
    return ChatResponse(
        content=content,
        model=data.get("model") or requested_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=Decimal(str(cost)) if cost is not None else None,
    )


def _error_body(response: httpx.Response) -> dict[str, Any]:
    """The error object of a failed call. Gemini's OpenAI-compatible endpoint wraps the
    google.rpc error in a one-item list."""
    try:
        data = response.json()
    except ValueError:
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else {}


def _details(response: httpx.Response) -> list[dict[str, Any]]:
    details = _error_body(response).get("details")
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _per_day_quota(response: httpx.Response) -> bool:
    """True when a violated quota is counted per day (Gemini quotaId "...PerDay..."), or the
    message says "per day"."""
    for detail in _details(response):
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict) and "PerDay" in str(violation.get("quotaId", "")):
                return True
    return "per day" in str(_error_body(response).get("message", "")).lower()


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the provider asked to wait: Gemini's RetryInfo.retryDelay ("22s"), else the
    Retry-After header (seconds or an HTTP date)."""
    for detail in _details(response):
        match = _DURATION.match(str(detail.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)
