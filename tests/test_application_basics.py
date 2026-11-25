import sqlite3

import pytest
from pydantic import ValidationError

from margin_guard.config import Settings
from margin_guard.database import initialize_database


def test_settings_use_defaults():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert str(settings.database_path) == "data/margin_guard.sqlite3"
    assert settings.monitoring_interval_minutes == 45


def test_monitoring_interval_is_limited_to_thirty_through_sixty_minutes():
    for value in (29, 61):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, monitoring_interval_minutes=value)


def test_database_initialization_creates_user_and_product_tables(tmp_path):
    database_path = tmp_path / "nested" / "app.sqlite3"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"users", "products"} <= tables
