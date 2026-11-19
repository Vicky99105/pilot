from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.health.contract import ComponentStatus
from tests.fakes import FakeHealthService


def _settings(ui_dist_dir: Path | None = None) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://user:pass@127.0.0.1:1/none",
        temporal_address="127.0.0.1:1",
        ui_dist_dir=ui_dist_dir,
    )


def test_app_starts_and_serves_health_when_temporal_unreachable() -> None:
    service = FakeHealthService(temporal=ComponentStatus(status="down", detail="unreachable"))
    app = create_app(_settings(), health_service=service)

    # Entering the client runs the lifespan, which starts the worker loop.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["components"]["temporal"]["status"] == "down"


def test_serves_ui_index_from_dist_dir_when_it_exists(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<title>ConsintIQ Claims Pilot</title>")
    app = create_app(_settings(tmp_path), health_service=FakeHealthService())

    with TestClient(app) as client:
        index = client.get("/")
        health = client.get("/health")

    assert index.status_code == 200
    assert "ConsintIQ Claims Pilot" in index.text
    assert health.status_code == 200


def test_does_not_serve_ui_when_dist_dir_missing(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path / "missing"), health_service=FakeHealthService())

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 404
