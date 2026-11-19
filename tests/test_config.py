import pytest

from app.config import Settings


def test_settings_read_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/claims")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "pilot")
    monkeypatch.setenv("UI_DIST_DIR", "/srv/ui")

    settings = Settings()

    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/claims"
    assert settings.temporal_address == "temporal:7233"
    assert settings.temporal_namespace == "pilot"
    assert str(settings.ui_dist_dir) == "/srv/ui"


def test_task_queue_defaults_to_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE", raising=False)

    assert Settings().temporal_task_queue == "claims"
