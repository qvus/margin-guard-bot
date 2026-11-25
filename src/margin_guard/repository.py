"""Small SQLite operations used by the Telegram command handlers."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from .database import connect


def get_or_create_user(database_path: str | Path, telegram_id: int) -> sqlite3.Row:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (telegram_id) VALUES (?) "
            "ON CONFLICT(telegram_id) DO NOTHING",
            (telegram_id,),
        )
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("Could not create user")
        return row


def get_user(database_path: str | Path, telegram_id: int) -> sqlite3.Row | None:
    with connect(database_path) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def save_encrypted_token(
    database_path: str | Path, telegram_id: int, encrypted_token: str
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (telegram_id, wb_token_encrypted) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET wb_token_encrypted = excluded.wb_token_encrypted",
            (telegram_id, encrypted_token),
        )


def set_threshold(database_path: str | Path, telegram_id: int, threshold: str) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (telegram_id, margin_threshold) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET margin_threshold = excluded.margin_threshold",
            (telegram_id, threshold),
        )


def save_products(
    database_path: str | Path, user_id: int, products: list[dict[str, Any]]
) -> int:
    """Cache current WB product data while preserving costs already entered."""
    saved = 0
    with connect(database_path) as connection:
        for product in products:
            card = product.get("card")
            if not isinstance(card, dict) or card.get("nmID") is None:
                continue
            product_id = str(card["nmID"])
            connection.execute(
                """INSERT INTO products (
                       user_id, marketplace_product_id, vendor_code, title, product_data_json
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, marketplace, marketplace_product_id) DO UPDATE SET
                       vendor_code = excluded.vendor_code,
                       title = excluded.title,
                       product_data_json = excluded.product_data_json""",
                (
                    user_id,
                    product_id,
                    card.get("vendorCode"),
                    card.get("title"),
                    json.dumps(product, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            saved += 1
    return saved


def list_products(database_path: str | Path, user_id: int) -> list[sqlite3.Row]:
    with connect(database_path) as connection:
        return connection.execute(
            """SELECT marketplace_product_id, vendor_code, title, product_data_json, cost_price
               FROM products WHERE user_id = ? AND marketplace = 'wildberries'
               ORDER BY title, marketplace_product_id""",
            (user_id,),
        ).fetchall()


def set_product_cost(
    database_path: str | Path, user_id: int, nm_id: str, cost: str
) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """UPDATE products SET cost_price = ?
               WHERE user_id = ? AND marketplace = 'wildberries'
                 AND marketplace_product_id = ?""",
            (cost, user_id, nm_id),
        )
        return cursor.rowcount > 0


def delete_user(database_path: str | Path, telegram_id: int) -> bool:
    with connect(database_path) as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if user is None:
            return False
        # Explicitly remove child rows as well, so deletion works for databases
        # created with the earlier schema that did not declare ON DELETE CASCADE.
        connection.execute("DELETE FROM products WHERE user_id = ?", (user["id"],))
        cursor = connection.execute(
            "DELETE FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return cursor.rowcount > 0
