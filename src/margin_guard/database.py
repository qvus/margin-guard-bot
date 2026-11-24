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
    return connection


def initialize_database(database_path: str | Path) -> None:
    """Create the minimal user and product tables if they do not exist yet."""
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL DEFAULT 'wildberries',
                marketplace_product_id TEXT NOT NULL,
                cost_price TEXT,
                UNIQUE (user_id, marketplace, marketplace_product_id)
            );
            """
        )
