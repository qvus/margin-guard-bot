"""Immutable validated application settings loaded from environment or .env."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_telegram_id_allowlist(value: Any) -> frozenset[int]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, (set, frozenset, list, tuple)):
        return frozenset(int(item) for item in value)
    if isinstance(value, int):
        return frozenset({value})
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            return frozenset()
        parsed: set[int] = set()
        for part in parts:
            uid = int(part)
            if uid <= 0:
                raise ValueError("Telegram user IDs must be positive integers")
            parsed.add(uid)
        return frozenset(parsed)
    raise ValueError("Invalid ALLOWED_TELEGRAM_IDS format")


TelegramAllowlist = Annotated[
    frozenset[int],
    NoDecode,
    BeforeValidator(_parse_telegram_id_allowlist),
]


class Settings(BaseSettings):
    """Application settings with strict bounds and secret encapsulation."""

    app_env: Literal["development", "staging", "production", "test"] = "development"
    database_path: Path = Path("data/margin_guard.sqlite3")
    telegram_bot_token: SecretStr | None = None
    token_encryption_key: SecretStr | None = None
    monitoring_interval_minutes: int = Field(default=45, ge=30, le=60)
    allowed_telegram_ids: TelegramAllowlist = Field(default_factory=frozenset)
    wb_request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    wb_max_retries: int = Field(default=3, ge=0, le=5)
    monitoring_max_concurrency: int = Field(default=4, ge=1, le=20)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    @field_validator("database_path")
    @classmethod
    def _validate_database_path(cls, path: Path) -> Path:
        raw = str(path)
        if "\x00" in raw or not raw.strip():
            raise ValueError("database_path must be a valid non-empty path")
        return path


def get_settings() -> Settings:
    """Load validated settings from process environment / .env."""
    return Settings()
