"""Application settings loaded from environment variables or a local .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Small stage-one configuration; secrets are added with the bot integration."""

    app_env: str = "development"
    database_path: Path = Path("data/margin_guard.sqlite3")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_settings() -> Settings:
    """Create settings from the current process environment."""
    return Settings()
