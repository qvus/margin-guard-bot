"""Application settings loaded from environment variables or a local .env file."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    app_env: str = "development"
    database_path: Path = Path("data/margin_guard.sqlite3")
    telegram_bot_token: SecretStr | None = None
    token_encryption_key: SecretStr | None = None
    monitoring_interval_minutes: int = Field(default=45, ge=30, le=60)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_settings() -> Settings:
    """Create settings from the current process environment."""
    return Settings()
