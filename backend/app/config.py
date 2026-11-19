"""Runtime settings, read from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # No env_file: the compose file passes each variable explicitly.
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+psycopg://claims:claims@localhost:5432/claims"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "claims"
    ui_dist_dir: Path | None = None
