import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ModelCallRow
from app.integrations.openai_compatible import OpenAICompatibleProvider
from app.services.model_gateway.contract import (
    Message,
    ModelGateway,
    ModelOutputInvalid,
    ModelQuotaExhausted,
    ModelRole,
    ModelUnavailable,
)
from app.services.model_gateway.service import (
    DefaultModelGateway,
    ModelsConfig,
    load_models_config,
)
from app.shared.errors import PermanentError
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY = "sk-or-test-key-must-never-be-logged"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class DocLabel(BaseModel):
    doc_type: str
    confidence: float


def _completion(content: str, cost: float | None = 0.00042) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": 15}
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": "google/gemini-2.5-flash",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage,
    }


class ScriptedTransport:
    """Fake HTTP transport: replies with scripted responses and records requests."""

    def __init__(self, replies: list[httpx.Response]) -> None:
        self.replies = replies
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json=_completion(content))


GatewayFactory = Callable[[list[httpx.Response]], tuple[ModelGateway, ScriptedTransport]]


@pytest.fixture
def config() -> ModelsConfig:
    """A test config, independent of which provider config/models.yaml currently selects."""
    role = {"provider": "openrouter", "model": "google/gemini-2.5-flash", "params": {}}
    return ModelsConfig.model_validate(
        {
            "providers": {
                "openrouter": {
                    "kind": "openai_compatible",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "request_extras": {"usage": {"include": True}},
                    "pricing": {
                        "google/gemini-2.5-flash": {
                            "input_per_mtok": "0.30",
                            "output_per_mtok": "2.50",
                        }
                    },
                }
            },
            "roles": {r.value: role for r in ModelRole},
        }
    )


@pytest.fixture
def file_store() -> FakeFileStore:
    return FakeFileStore()


@pytest.fixture
def make_gateway(
    config: ModelsConfig,
    sessions: async_sessionmaker[AsyncSession],
    file_store: FakeFileStore,
) -> GatewayFactory:
    def build(replies: list[httpx.Response]) -> tuple[ModelGateway, ScriptedTransport]:
        transport = ScriptedTransport(replies)
        provider = OpenAICompatibleProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=SecretStr(API_KEY),
            http=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
            extra_body=config.providers["openrouter"].request_extras,
        )
        gateway = DefaultModelGateway(
            config=config,
            providers={"openrouter": provider},
            file_store=file_store,
            sessions=sessions,
        )
        return gateway, transport

    return build


async def _calls(sessions: async_sessionmaker[AsyncSession]) -> list[ModelCallRow]:
    async with sessions() as session:
        return list(
            (await session.scalars(select(ModelCallRow).order_by(ModelCallRow.attempt))).all()
        )


def test_repo_models_config_maps_every_role_to_a_configured_provider() -> None:
    repo = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert set(repo.roles) == set(ModelRole)
    assert {r.provider for r in repo.roles.values()} <= set(repo.providers)
    assert repo.providers["openrouter"].api_key_env == "OPENROUTER_API_KEY"
    assert repo.providers["gemini"].api_key_env == "GEMINI_API_KEY"
    for role in repo.roles.values():
        assert role.model in repo.providers[role.provider].pricing, role.model


async def test_provider_without_request_extras_sends_no_usage_field() -> None:
    transport = ScriptedTransport([_ok('{"doc_type": "x", "confidence": 1}')])
    provider = OpenAICompatibleProvider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key=SecretStr(API_KEY),
        http=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
    )
    await provider.chat_json(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        schema_name="DocLabel",
        json_schema=DocLabel.model_json_schema(),
        params={},
    )
    body = transport.bodies()[0]
    assert "usage" not in body
    assert str(transport.requests[0].url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )


async def test_cost_is_estimated_from_pricing_when_provider_reports_none(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    reply = httpx.Response(200, json=_completion('{"doc_type": "x", "confidence": 1}', cost=None))
    gateway, transport = make_gateway([reply])

    await gateway.structured(
        ModelRole.FAST_TEXT,
        [Message(role="user", content="hi")],
        DocLabel,
        claim_id=None,
        purpose="p",
    )

    assert transport.bodies()[0]["usage"] == {"include": True}
    [call] = await _calls(sessions)
    # 120 input tokens at 0.30 per million plus 15 output tokens at 2.50 per million
    # the cost column keeps 6 decimal places, so the stored value is within 0.000001 of it
    assert call.cost is not None
    assert abs(call.cost - Decimal("0.0000735")) <= Decimal("0.000001")


async def test_structured_returns_parsed_output_and_logs_the_call(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    gateway, transport = make_gateway([_ok('{"doc_type": "final_bill", "confidence": 0.93}')])
    claim_id = uuid4()

    result = await gateway.structured(
        ModelRole.FAST_TEXT,
        [Message(role="user", content="Label this page")],
        DocLabel,
        claim_id=claim_id,
        purpose="classify_page",
    )

    assert result == DocLabel(doc_type="final_bill", confidence=0.93)
    body = transport.bodies()[0]
    assert body["model"] == "google/gemini-2.5-flash"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["title"] == "DocLabel"
    assert transport.requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    [call] = await _calls(sessions)
    assert (call.claim_id, call.purpose, call.role) == (claim_id, "classify_page", "fast_text")
    assert (call.model, call.provider, call.valid, call.attempt) == (
        "google/gemini-2.5-flash",
        "openrouter",
        True,
        1,
    )
    assert (call.input_tokens, call.output_tokens) == (120, 15)
    assert call.cost == Decimal("0.000420")
    assert call.latency_ms >= 0
    assert call.prompt == [{"role": "user", "content": "Label this page"}]


async def test_retries_once_when_output_fails_schema_validation(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    gateway, transport = make_gateway(
        [_ok('{"doc_type": "final_bill"}'), _ok('{"doc_type": "final_bill", "confidence": 0.8}')]
    )

    result = await gateway.structured(
        ModelRole.FAST_TEXT,
        [Message(role="user", content="Label")],
        DocLabel,
        claim_id=None,
        purpose="classify_page",
    )

    assert result.confidence == 0.8
    retry_messages = transport.bodies()[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": '{"doc_type": "final_bill"}'}
    assert "did not match the JSON schema" in retry_messages[-1]["content"]
    first, second = await _calls(sessions)
    assert (first.valid, first.attempt, second.valid, second.attempt) == (False, 1, True, 2)
    assert first.error is not None and "confidence" in first.error


async def test_raises_after_second_invalid_output(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    gateway, _ = make_gateway([_ok("not json"), _ok('{"doc_type": 3}')])

    with pytest.raises(ModelOutputInvalid):
        await gateway.structured(
            ModelRole.FAST_TEXT,
            [Message(role="user", content="Label")],
            DocLabel,
            claim_id=None,
            purpose="classify_page",
        )

    assert [c.valid for c in await _calls(sessions)] == [False, False]
    assert issubclass(ModelOutputInvalid, PermanentError)


async def test_vision_stores_images_and_logs_keys_not_bytes(
    make_gateway: GatewayFactory,
    sessions: async_sessionmaker[AsyncSession],
    file_store: FakeFileStore,
) -> None:
    gateway, transport = make_gateway([_ok('{"doc_type": "claim_form", "confidence": 0.7}')])

    result = await gateway.vision(
        ModelRole.VISION_CLASSIFY,
        "What document is this page?",
        [PNG],
        DocLabel,
        claim_id=None,
        purpose="classify_page",
    )

    assert result.doc_type == "claim_form"
    [image_key] = file_store.files
    content = transport.bodies()[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What document is this page?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    [call] = await _calls(sessions)
    assert call.image_keys == [image_key]
    assert "base64" not in json.dumps(call.prompt)
    assert {"type": "image_ref", "key": image_key} in call.prompt[0]["content"]


async def test_api_key_never_reaches_the_call_log(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    gateway, _ = make_gateway([httpx.Response(401, json={"error": {"message": "bad key"}})])

    with pytest.raises(PermanentError):
        await gateway.structured(
            ModelRole.REASONING,
            [Message(role="user", content="x")],
            DocLabel,
            claim_id=None,
            purpose="p",
        )

    [call] = await _calls(sessions)
    row_text = json.dumps({c.key: str(getattr(call, c.key)) for c in call.__table__.columns})
    assert API_KEY not in row_text
    assert call.valid is False


async def test_server_error_raises_retryable_model_unavailable(
    make_gateway: GatewayFactory,
) -> None:
    gateway, _ = make_gateway([httpx.Response(503, text="overloaded")])

    with pytest.raises(ModelUnavailable):
        await gateway.structured(
            ModelRole.REASONING,
            [Message(role="user", content="x")],
            DocLabel,
            claim_id=None,
            purpose="p",
        )

    assert not issubclass(ModelUnavailable, PermanentError)


async def test_fake_gateway_returns_scripted_responses_by_purpose() -> None:
    gateway: ModelGateway = FakeModelGateway(
        {"classify_page": [{"doc_type": "final_bill", "confidence": 0.9}]}
    )

    result = await gateway.vision(
        ModelRole.VISION_CLASSIFY, "p", [PNG], DocLabel, claim_id=None, purpose="classify_page"
    )

    assert result == DocLabel(doc_type="final_bill", confidence=0.9)


async def test_thinking_tokens_count_as_output_tokens() -> None:
    """Gemini bills thinking tokens as output but reports them outside completion_tokens."""
    completion = _completion('{"doc_type": "x", "confidence": 1}', cost=None)
    completion["usage"] = {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 255}
    transport = ScriptedTransport([httpx.Response(200, json=completion)])
    provider = OpenAICompatibleProvider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key=SecretStr(API_KEY),
        http=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
    )
    reply = await provider.chat_json(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        schema_name="DocLabel",
        json_schema=DocLabel.model_json_schema(),
        params={},
    )
    assert (reply.input_tokens, reply.output_tokens) == (9, 246)


def _gemini_429(*details: dict[str, Any], message: str = "Resource has been exhausted") -> Any:
    """Gemini's OpenAI-compatible endpoint wraps the google.rpc error in a list."""
    return [
        {
            "error": {
                "code": 429,
                "message": message,
                "status": "RESOURCE_EXHAUSTED",
                "details": list(details),
            }
        }
    ]


RETRY_INFO = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "22s"}


def _quota(quota_id: str) -> dict[str, Any]:
    return {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [
            {
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
                "quotaId": quota_id,
            }
        ],
    }


async def _call(gateway: ModelGateway) -> None:
    await gateway.structured(
        ModelRole.REASONING,
        [Message(role="user", content="x")],
        DocLabel,
        claim_id=None,
        purpose="p",
    )


async def test_rate_limit_carries_the_retry_delay_gemini_sends(
    make_gateway: GatewayFactory,
) -> None:
    gateway, _ = make_gateway(
        [
            httpx.Response(
                429, json=_gemini_429(_quota("GenerateRequestsPerMinutePerProject"), RETRY_INFO)
            )
        ]
    )

    with pytest.raises(ModelUnavailable) as raised:
        await _call(gateway)

    assert raised.value.retry_after_seconds == 22.0


async def test_rate_limit_reads_the_retry_after_header(make_gateway: GatewayFactory) -> None:
    gateway, _ = make_gateway(
        [httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": 429}})]
    )

    with pytest.raises(ModelUnavailable) as raised:
        await _call(gateway)

    assert raised.value.retry_after_seconds == 7.0


async def test_server_error_without_a_hint_has_no_retry_delay(
    make_gateway: GatewayFactory,
) -> None:
    gateway, _ = make_gateway([httpx.Response(503, text="overloaded")])

    with pytest.raises(ModelUnavailable) as raised:
        await _call(gateway)

    assert raised.value.retry_after_seconds is None


async def test_daily_quota_exhaustion_is_permanent(
    make_gateway: GatewayFactory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    gateway, _ = make_gateway(
        [
            httpx.Response(
                429,
                json=_gemini_429(
                    _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier"), RETRY_INFO
                ),
            )
        ]
    )

    with pytest.raises(ModelQuotaExhausted) as raised:
        await _call(gateway)

    assert isinstance(raised.value, PermanentError)
    assert "per day" in str(raised.value)
    [call] = await _calls(sessions)
    assert call.valid is False


async def test_quota_message_that_says_per_day_is_permanent(
    make_gateway: GatewayFactory,
) -> None:
    gateway, _ = make_gateway(
        [
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded: requests per day for this model.",
                    }
                },
            )
        ]
    )

    with pytest.raises(ModelQuotaExhausted):
        await _call(gateway)


# --- Per-role model override (P3-2: lets the eval compare models without editing models.yaml)


def test_role_override_swaps_provider_and_model_and_keeps_the_role_params() -> None:
    from app.services.model_gateway.service import apply_role_overrides

    repo = load_models_config(REPO_ROOT / "config" / "models.yaml")
    changed = apply_role_overrides(
        repo,
        "reasoning=openrouter:anthropic/claude-sonnet-4.5, vision_extract=gemini:gemini-3.8-flash",
    )
    assert changed.roles[ModelRole.REASONING].provider == "openrouter"
    assert changed.roles[ModelRole.REASONING].model == "anthropic/claude-sonnet-4.5"
    assert changed.roles[ModelRole.REASONING].params == repo.roles[ModelRole.REASONING].params
    assert changed.roles[ModelRole.VISION_EXTRACT].model == "gemini-3.8-flash"
    assert changed.roles[ModelRole.FAST_TEXT] == repo.roles[ModelRole.FAST_TEXT]
    assert repo.roles[ModelRole.REASONING].provider == "gemini"  # the input is not changed


def test_an_empty_override_changes_nothing() -> None:
    from app.services.model_gateway.service import apply_role_overrides

    repo = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert apply_role_overrides(repo, "") == repo
    assert apply_role_overrides(repo, None) == repo


@pytest.mark.parametrize(
    "spec",
    ["nonsense=gemini:x", "reasoning=nowhere:x", "reasoning", "reasoning=gemini:", "reasoning=:x"],
)
def test_a_bad_override_fails_loudly(spec: str) -> None:
    from app.services.model_gateway.service import apply_role_overrides

    repo = load_models_config(REPO_ROOT / "config" / "models.yaml")
    with pytest.raises(ValueError):
        apply_role_overrides(repo, spec)


def test_settings_read_the_override_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("MODEL_ROLE_OVERRIDES", "fast_text=gemini:gemini-3.8-flash")
    assert Settings().model_role_overrides == "fast_text=gemini:gemini-3.8-flash"
