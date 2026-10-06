"""Application settings, loaded from environment variables (or a local .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Bulk Certificate Generator"

    # Persistence
    database_url: str = "sqlite:///./data/certificates.db"
    storage_dir: Path = Path("./data/certificates")

    # Background processing
    celery_broker_url: str = "redis://localhost:6379/0"
    # When true, Celery tasks run inline inside the API process (no broker/worker needed).
    # Useful for local development and tests; never use it in production.
    celery_task_always_eager: bool = False
    # Number of certificates handled by one Celery task.
    chunk_size: int = Field(default=50, ge=1, le=1000)
    # A certificate stuck in "processing" longer than this (e.g. its worker crashed)
    # may be claimed again by another worker.
    processing_lease_seconds: int = Field(default=300, ge=10)

    # Limits
    max_recipients_per_job: int = Field(default=10_000, ge=1)
    max_csv_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)

    # Optional shared-secret auth. When set, clients must send `X-API-Key`.
    api_key: str | None = None

    # Used to build the verification URL embedded in each certificate's QR code.
    public_base_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
