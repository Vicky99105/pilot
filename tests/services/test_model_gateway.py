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
