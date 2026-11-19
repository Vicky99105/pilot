"""Runtime settings, read from environment variables."""

from datetime import timedelta
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
    # Test-data admin operations on the mock core (reset a policy's balances), for local
    # acceptance runs only. Off by default; only deploy/docker-compose.local.yml turns it on.
    mock_core_admin_enabled: bool = False
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "claims"
    ui_dist_dir: Path | None = None
    config_dir: Path = _ROOT / "config"
    file_store_dir: Path = Path("/data/files")
    # Base64 of 32 random bytes. Required before any file is stored.
    file_encryption_key: SecretStr | None = None
    # Letters by email. Off by default (shadow mode): letters are recorded, never sent. When
    # on, only addresses in NOTIFICATION_ALLOWLIST (comma-separated) can receive mail.
    notifications_graph_enabled: bool = False
    notification_allowlist: str = ""
    graph_sender: str | None = None
    graph_access_token: SecretStr | None = None
    # Query loop: how long the claimant has to reply, and when to remind them, as offsets
    # from raising the query. ISO 8601 durations in the environment, e.g. QUERY_REPLY_WINDOW
    # =PT6M and QUERY_REMINDERS='["PT2M","PT4M"]' for an eval run in minutes.
    query_reply_window: timedelta = timedelta(days=15)
    query_reminders: list[timedelta] = [timedelta(days=3), timedelta(days=7)]
    # Evals only: "role=provider:model,..." repoints model roles without editing
    # config/models.yaml (see model_gateway.service.apply_role_overrides). Empty in production.
    model_role_overrides: str = ""
