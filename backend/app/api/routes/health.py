import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.services.health.contract import ComponentStatus, HealthService

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    components: dict[str, ComponentStatus]


def get_health_service(request: Request) -> HealthService:
    service: HealthService = request.app.state.health_service
    return service


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response, service: Annotated[HealthService, Depends(get_health_service)]
) -> HealthResponse:
    database, temporal = await asyncio.gather(service.check_database(), service.check_temporal())
    components = {"database": database, "temporal": temporal}
    all_ok = all(c.status == "ok" for c in components.values())
    response.status_code = 200 if all_ok else 503
    return HealthResponse(status="ok" if all_ok else "degraded", components=components)
