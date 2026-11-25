"""Hardened SQLite connection management and schema migrations."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
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

_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_users_active_monitoring
    ON users (monitoring_enabled)
    WHERE monitoring_enabled = 1 AND wb_token_encrypted IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_products_user_marketplace
    ON products (user_id, marketplace);
"""

_USER_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("wb_token_encrypted", "ALTER TABLE users ADD COLUMN wb_token_encrypted TEXT"),
    (
        "margin_threshold",
        "ALTER TABLE users ADD COLUMN margin_threshold TEXT NOT NULL DEFAULT '0'",
    ),
    (
        "expense_defaults_json",
        "ALTER TABLE users ADD COLUMN expense_defaults_json TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        "monitoring_enabled",
        "ALTER TABLE users ADD COLUMN monitoring_enabled INTEGER NOT NULL DEFAULT 1",
    ),
)

_PRODUCT_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("vendor_code", "ALTER TABLE products ADD COLUMN vendor_code TEXT"),
    ("title", "ALTER TABLE products ADD COLUMN title TEXT"),
    ("product_data_json", "ALTER TABLE products ADD COLUMN product_data_json TEXT"),
    (
        "expense_overrides_json",
        "ALTER TABLE products ADD COLUMN expense_overrides_json TEXT NOT NULL DEFAULT '{}'",
    ),
    ("last_margin_state", "ALTER TABLE products ADD COLUMN last_margin_state TEXT"),
    ("last_margin", "ALTER TABLE products ADD COLUMN last_margin TEXT"),
    ("last_price", "ALTER TABLE products ADD COLUMN last_price TEXT"),
)


class SqliteDatabase:
    """Manage SQLite connections with guaranteed close, WAL mode, and strict POSIX permissions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._raw_path = str(database_path)
        self._is_memory = self._raw_path == ":memory:"
        self._busy_timeout_ms = max(100, busy_timeout_ms)

    @property
    def path(self) -> str:
        return self._raw_path

    def initialize(self) -> None:
        """Create or migrate tables and enforce owner-only file permissions."""
        self._prepare_filesystem()
        with self.connection() as conn:
            if not self._is_memory:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA_SQL)
            existing_users = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(users)")
            }
            for col_name, alter_stmt in _USER_MIGRATIONS:
                if col_name not in existing_users:
                    conn.execute(alter_stmt)

            existing_products = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(products)")
            }
            for col_name, alter_stmt in _PRODUCT_MIGRATIONS:
                if col_name not in existing_products:
                    conn.execute(alter_stmt)

            conn.executescript(_INDEXES_SQL)
        self._harden_file_permissions()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a transactional SQLite connection and always close its descriptor on exit."""
        self._prepare_filesystem()
        conn = sqlite3.connect(self._raw_path, timeout=self._busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            conn.execute("PRAGMA temp_store = MEMORY")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _prepare_filesystem(self) -> None:
        if self._is_memory:
            return
        db_file = Path(self._raw_path)
        parent = db_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            with suppress(OSError):
                os.chmod(parent, 0o700)

    def _harden_file_permissions(self) -> None:
        if self._is_memory or os.name != "posix":
            return
        base = Path(self._raw_path)
        for candidate in (
            base,
            Path(f"{self._raw_path}-wal"),
            Path(f"{self._raw_path}-shm"),
        ):
            if candidate.exists():
                with suppress(OSError):
                    os.chmod(candidate, 0o600)
