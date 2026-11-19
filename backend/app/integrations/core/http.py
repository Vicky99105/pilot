"""HTTP clients for the core contracts. The base URL points at /mock-core in Phase 1."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from app.integrations.core.contract import (
    BlockNotFound,
    BlockRef,
    CoreRequestError,
    CoreUnavailable,
    InsufficientSumInsured,
    MemberPolicy,
    Payee,
    PaymentRef,
)


class _CoreClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: Any = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, self._base + path, json=json, params=params)
        except httpx.TransportError as exc:
            raise CoreUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 500:
            raise CoreUnavailable(f"HTTP {response.status_code}: {response.text[:300]}")
        return response


def _refused(response: httpx.Response) -> CoreRequestError:
    return CoreRequestError(f"HTTP {response.status_code}: {response.text[:300]}")


class HttpPolicyService(_CoreClient):
    async def find_member(
        self, *, policy_number: str | None, member_id: str | None
    ) -> MemberPolicy | None:
        params = {
            k: v for k, v in {"policy_number": policy_number, "member_id": member_id}.items() if v
        }
        response = await self._request("GET", "/members", params=params)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _refused(response)
        return MemberPolicy.model_validate(response.json())


class HttpMoneyService(_CoreClient):
    async def block_sum_insured(
        self, *, policy_number: str, member_id: str, amount: Decimal, idempotency_key: str
    ) -> BlockRef:
        response = await self._request(
            "POST",
            "/blocks",
            json={
                "policy_number": policy_number,
                "member_id": member_id,
                "amount": str(amount),
                "idempotency_key": idempotency_key,
            },
        )
        if response.status_code == 409:
            raise InsufficientSumInsured(response.json().get("detail", ""))
        if response.status_code != 200:
            raise _refused(response)
        return BlockRef.model_validate(response.json())

    async def release_block(
        self, *, block_id: str, utilised: Decimal, idempotency_key: str
    ) -> None:
        response = await self._request(
            "POST",
            f"/blocks/{block_id}/release",
            json={"utilised": str(utilised), "idempotency_key": idempotency_key},
        )
        if response.status_code == 404:
            raise BlockNotFound(block_id)
        if response.status_code != 200:
            raise _refused(response)

    async def create_payment_instruction(
        self, *, payee: Payee, amount: Decimal, claim_number: str, idempotency_key: str
    ) -> PaymentRef:
        response = await self._request(
            "POST",
            "/payments",
            json={
                "payee": payee.model_dump(mode="json"),
                "amount": str(amount),
                "claim_number": claim_number,
                "idempotency_key": idempotency_key,
            },
        )
        if response.status_code != 200:
            raise _refused(response)
        return PaymentRef.model_validate(response.json())


class HttpPermissionService(_CoreClient):
    async def can_submit_for(
        self, *, user_id: UUID, policy_number: str | None, hospital_id: str | None
    ) -> bool:
        response = await self._request(
            "POST",
            "/permissions/can-submit",
            json={
                "user_id": str(user_id),
                "policy_number": policy_number,
                "hospital_id": hospital_id,
            },
        )
        if response.status_code != 200:
            raise _refused(response)
        return bool(response.json()["allowed"])
