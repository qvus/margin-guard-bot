"""SQLite connection and initial schema for the application."""

import sqlite3
from pathlib import Path


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection, creating the parent folder for file databases."""
    path = str(database_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: str | Path) -> None:
    """Create the minimal user and product tables if they do not exist yet."""
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                wb_token_encrypted TEXT,
                margin_threshold TEXT NOT NULL DEFAULT '0',
                expense_defaults_json TEXT NOT NULL DEFAULT '{}',
                monitoring_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL DEFAULT 'wildberries',
                marketplace_product_id TEXT NOT NULL,
                vendor_code TEXT,
                title TEXT,
                product_data_json TEXT,
                cost_price TEXT,
                expense_overrides_json TEXT NOT NULL DEFAULT '{}',
                last_margin_state TEXT,
                last_margin TEXT,
                last_price TEXT,
                UNIQUE (user_id, marketplace, marketplace_product_id)
            );
            """
        )
        # Add stage-three columns to databases created by the stage-one schema.
        _ensure_columns(
            connection,
            "users",
            {
                "wb_token_encrypted": "TEXT",
                "margin_threshold": "TEXT NOT NULL DEFAULT '0'",
                "expense_defaults_json": "TEXT NOT NULL DEFAULT '{}'",
                "monitoring_enabled": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        _ensure_columns(
            connection,
            "products",
            {
                "vendor_code": "TEXT",
                "title": "TEXT",
                "product_data_json": "TEXT",
                "expense_overrides_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_margin_state": "TEXT",
                "last_margin": "TEXT",
                "last_price": "TEXT",
            },
        )


def _ensure_columns(
    connection: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
