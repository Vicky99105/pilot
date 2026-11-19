"""Runtime settings, read from environment variables."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> repo root locally; /srv/backend/app/config.py -> /srv in the image.
_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # No env_file: the compose file passes each variable explicitly.
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+psycopg://claims:claims@localhost:5432/claims"
    mock_core_database_url: str = "postgresql+psycopg://claims:claims@localhost:5432/mock_core"
    # Where the Http core clients reach the core systems. Phase 1: this app's own router.
    mock_core_base_url: str = "http://127.0.0.1:8000/mock-core"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "claims"
    ui_dist_dir: Path | None = None
    config_dir: Path = _ROOT / "config"
    file_store_dir: Path = Path("/data/files")
    # Base64 of 32 random bytes. Required before any file is stored.
    file_encryption_key: SecretStr | None = None
