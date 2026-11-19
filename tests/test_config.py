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


def test_settings_read_phase_1_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_CORE_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/mock_core")
    monkeypatch.setenv("MOCK_CORE_BASE_URL", "http://127.0.0.1:8000/mock-core")
    monkeypatch.setenv("FILE_STORE_DIR", "/data/files")
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "a2V5")

    settings = Settings()

    assert settings.mock_core_database_url.endswith("/mock_core")
    assert settings.mock_core_base_url == "http://127.0.0.1:8000/mock-core"
    assert str(settings.file_store_dir) == "/data/files"
    assert settings.file_encryption_key is not None
    assert settings.file_encryption_key.get_secret_value() == "a2V5"
    assert "a2V5" not in repr(settings)


def test_config_dir_holds_models_yaml() -> None:
    assert (Settings().config_dir / "models.yaml").is_file()
