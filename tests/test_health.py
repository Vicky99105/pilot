import httpx

from app.config import Settings
from app.main import create_app
from app.services.health.contract import ComponentStatus
from tests.fakes import FakeHealthService

UNREACHABLE = Settings(
    database_url="postgresql+psycopg://user:pass@127.0.0.1:1/none",
    temporal_address="127.0.0.1:1",
)


async def _get_health(service: FakeHealthService) -> httpx.Response:
    app = create_app(UNREACHABLE, health_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health")


async def test_health_returns_200_and_ok_when_all_components_ok() -> None:
    response = await _get_health(FakeHealthService())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "components": {
            "database": {"status": "ok", "detail": None},
            "temporal": {"status": "ok", "detail": None},
        },
    }


async def test_health_returns_503_and_degraded_when_temporal_down() -> None:
    service = FakeHealthService(temporal=ComponentStatus(status="down", detail="refused"))

    response = await _get_health(service)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["temporal"] == {"status": "down", "detail": "refused"}
    assert body["components"]["database"]["status"] == "ok"


async def test_health_returns_503_and_degraded_when_database_down() -> None:
    service = FakeHealthService(database=ComponentStatus(status="down", detail="timeout"))

    response = await _get_health(service)

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
