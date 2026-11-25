"""SQLite implementations of UserRepositoryPort and ProductRepositoryPort."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from margin_guard.domain.entities import (
    CatalogProduct,
    ProductSizePrice,
    SellerAccount,
    StoredProduct,
)
from margin_guard.domain.value_objects import (
    ALL_EXPENSE_CATEGORIES,
    ZERO,
    ExpenseCategory,
    ExpenseRule,
    MarginState,
    format_decimal,
    sanitize_display_text,
)
from margin_guard.infrastructure.persistence.sqlite_db import SqliteDatabase

MAX_CACHED_PRODUCTS_PER_SELLER = 10_000


def _parse_safe_decimal(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite():
        return None
    return ZERO if value == ZERO else value


def _deserialize_expenses(raw_json: str | None) -> dict[ExpenseCategory, ExpenseRule]:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    categories_by_name = {cat.value: cat for cat in ALL_EXPENSE_CATEGORIES}
    result: dict[ExpenseCategory, ExpenseRule] = {}
    for key, raw_rule in parsed.items():
        cat = categories_by_name.get(str(key))
        if cat is None or not isinstance(raw_rule, dict):
            continue
        fixed = _parse_safe_decimal(raw_rule.get("fixed", "0"))
        percent = _parse_safe_decimal(raw_rule.get("percent", "0"))
        if fixed is None or percent is None:
            continue
        try:
            result[cat] = ExpenseRule(fixed=fixed, percent=percent)
        except (TypeError, ValueError):
            continue
    return result


def _serialize_expenses(expenses: Mapping[ExpenseCategory, ExpenseRule]) -> str:
    payload = {
        cat.value: {
            "fixed": format_decimal(rule.fixed),
            "percent": format_decimal(rule.percent),
        }
        for cat, rule in expenses.items()
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _serialize_minimized_product(product: CatalogProduct) -> str:
    """Persist only essential pricing metadata (GDPR / OWASP data minimization)."""
    sizes_payload = []
    for size in product.sizes[:50]:
        entry: dict[str, str | int] = {}
        if size.price is not None:
            entry["price"] = format_decimal(size.price)
        if size.discounted_price is not None:
            entry["discountedPrice"] = format_decimal(size.discounted_price)
        if size.discount_percent is not None:
            entry["discount"] = int(size.discount_percent)
        if size.chrt_id is not None:
            entry["chrtID"] = int(size.chrt_id)
        sizes_payload.append(entry)
    return json.dumps({"sizes": sizes_payload}, separators=(",", ":"), ensure_ascii=False)


def _deserialize_sizes(raw_json: str | None) -> tuple[ProductSizePrice, ...]:
    if not raw_json:
        return ()
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, dict):
        return ()

    # Support both minimized {"sizes": [...]} and legacy {"pricing": {"sizes": [...]}}
    raw_sizes = parsed.get("sizes")
    if not isinstance(raw_sizes, list):
        pricing = parsed.get("pricing")
        raw_sizes = pricing.get("sizes") if isinstance(pricing, dict) else None
    if not isinstance(raw_sizes, list):
        return ()

    sizes: list[ProductSizePrice] = []
    for item in raw_sizes[:50]:
        if not isinstance(item, dict):
            continue
        price = _parse_safe_decimal(item.get("price"))
        discounted = _parse_safe_decimal(item.get("discountedPrice"))
        raw_disc = item.get("discount")
        discount_pct = int(raw_disc) if isinstance(raw_disc, int) else None
        raw_chrt = item.get("chrtID")
        chrt_id = int(raw_chrt) if isinstance(raw_chrt, int) else None
        sizes.append(
            ProductSizePrice(
                price=price,
                discounted_price=discounted,
                discount_percent=discount_pct,
                chrt_id=chrt_id,
            )
        )
    return tuple(sizes)


class SqliteUserRepository:
    """SQLite-backed seller account repository with static parameterized SQL."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def get_or_create(self, telegram_id: int) -> SellerAccount:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id) VALUES (?) "
                "ON CONFLICT(telegram_id) DO NOTHING",
                (telegram_id,),
            )
            row = conn.execute(
                "SELECT id, telegram_id, wb_token_encrypted, margin_threshold, "
                "expense_defaults_json, monitoring_enabled "
                "FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by insert above
                raise RuntimeError("Failed to create or load seller record")
            return self._row_to_entity(row)

    def get_by_telegram_id(self, telegram_id: int) -> SellerAccount | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT id, telegram_id, wb_token_encrypted, margin_threshold, "
                "expense_defaults_json, monitoring_enabled "
                "FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            return self._row_to_entity(row) if row is not None else None

    def save_encrypted_token(self, telegram_id: int, encrypted_token: str) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, wb_token_encrypted, monitoring_enabled) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "wb_token_encrypted = excluded.wb_token_encrypted, "
                "monitoring_enabled = 1",
                (telegram_id, encrypted_token),
            )

    def set_threshold(self, telegram_id: int, threshold: Decimal) -> None:
        formatted = format_decimal(threshold)
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, margin_threshold) VALUES (?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "margin_threshold = excluded.margin_threshold",
                (telegram_id, formatted),
            )

    def update_default_expenses(
        self,
        telegram_id: int,
        updates: Mapping[ExpenseCategory, ExpenseRule | None],
    ) -> None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT expense_defaults_json FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,)
                )
                merged: dict[ExpenseCategory, ExpenseRule] = {}
            else:
                merged = _deserialize_expenses(row["expense_defaults_json"])

            for category, rule in updates.items():
                if rule is None:
                    merged.pop(category, None)
                else:
                    merged[category] = rule

            conn.execute(
                "UPDATE users SET expense_defaults_json = ? WHERE telegram_id = ?",
                (_serialize_expenses(merged), telegram_id),
            )

    def set_monitoring_enabled(self, telegram_id: int, enabled: bool) -> bool:
        with self._db.connection() as conn:
            cursor = conn.execute(
                "UPDATE users SET monitoring_enabled = ? WHERE telegram_id = ?",
                (1 if enabled else 0, telegram_id),
            )
            return cursor.rowcount > 0

    def list_active_monitoring_sellers(self) -> Sequence[SellerAccount]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, telegram_id, wb_token_encrypted, margin_threshold, "
                "expense_defaults_json, monitoring_enabled "
                "FROM users "
                "WHERE monitoring_enabled = 1 AND wb_token_encrypted IS NOT NULL"
            ).fetchall()
            return [self._row_to_entity(row) for row in rows]

    def delete_by_telegram_id(self, telegram_id: int) -> bool:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if row is None:
                return False
            # Explicit child deletion keeps compatibility with databases created
            # prior to foreign-key cascade enforcement.
            conn.execute("DELETE FROM products WHERE user_id = ?", (row["id"],))
            cursor = conn.execute(
                "DELETE FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> SellerAccount:
        threshold = _parse_safe_decimal(row["margin_threshold"])
        return SellerAccount(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            encrypted_wb_token=(
                str(row["wb_token_encrypted"])
                if row["wb_token_encrypted"] is not None
                else None
            ),
            margin_threshold=threshold if threshold is not None else ZERO,
            expense_defaults=_deserialize_expenses(row["expense_defaults_json"]),
            monitoring_enabled=bool(row["monitoring_enabled"]),
        )


class SqliteProductRepository:
    """SQLite-backed product cache repository with data minimization."""

    def __init__(
        self,
        db: SqliteDatabase,
        *,
        max_products_per_seller: int = MAX_CACHED_PRODUCTS_PER_SELLER,
    ) -> None:
        self._db = db
        self._max_products = max(1, max_products_per_seller)

    def upsert_catalog_products(
        self, user_id: int, products: Sequence[CatalogProduct]
    ) -> int:
        saved = 0
        with self._db.connection() as conn:
            for product in products[: self._max_products]:
                clean_vendor = (
                    sanitize_display_text(product.vendor_code, fallback="", max_length=100)
                    or None
                )
                clean_title = (
                    sanitize_display_text(product.title, fallback="", max_length=200)
                    or None
                )
                conn.execute(
                    """INSERT INTO products (
                           user_id, marketplace_product_id, vendor_code, title, product_data_json
                       ) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, marketplace, marketplace_product_id) DO UPDATE SET
                           vendor_code = excluded.vendor_code,
                           title = excluded.title,
                           product_data_json = excluded.product_data_json""",
                    (
                        user_id,
                        product.nm_id,
                        clean_vendor,
                        clean_title,
                        _serialize_minimized_product(product),
                    ),
                )
                saved += 1
        return saved

    def list_by_user(self, user_id: int) -> Sequence[StoredProduct]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """SELECT marketplace_product_id, vendor_code, title, product_data_json,
                          cost_price, expense_overrides_json, last_margin_state,
                          last_margin, last_price
                   FROM products
                   WHERE user_id = ? AND marketplace = 'wildberries'
                   ORDER BY title, marketplace_product_id""",
                (user_id,),
            ).fetchall()
            return [self._row_to_entity(row) for row in rows]

    def set_cost_price(self, user_id: int, nm_id: str, cost_price: Decimal) -> bool:
        with self._db.connection() as conn:
            cursor = conn.execute(
                """UPDATE products SET cost_price = ?
                   WHERE user_id = ? AND marketplace = 'wildberries'
                     AND marketplace_product_id = ?""",
                (format_decimal(cost_price), user_id, nm_id),
            )
            return cursor.rowcount > 0

    def update_expense_overrides(
        self,
        user_id: int,
        nm_id: str,
        updates: Mapping[ExpenseCategory, ExpenseRule | None],
    ) -> bool:
        with self._db.connection() as conn:
            row = conn.execute(
                """SELECT expense_overrides_json FROM products
                   WHERE user_id = ? AND marketplace = 'wildberries'
                     AND marketplace_product_id = ?""",
                (user_id, nm_id),
            ).fetchone()
            if row is None:
                return False

            merged = _deserialize_expenses(row["expense_overrides_json"])
            for category, rule in updates.items():
                if rule is None:
                    merged.pop(category, None)
                else:
                    merged[category] = rule

            conn.execute(
                """UPDATE products SET expense_overrides_json = ?
                   WHERE user_id = ? AND marketplace = 'wildberries'
                     AND marketplace_product_id = ?""",
                (_serialize_expenses(merged), user_id, nm_id),
            )
            return True

    def save_monitoring_state(
        self,
        user_id: int,
        nm_id: str,
        state: MarginState,
        margin: Decimal,
        price: Decimal,
    ) -> None:
        with self._db.connection() as conn:
            conn.execute(
                """UPDATE products
                   SET last_margin_state = ?, last_margin = ?, last_price = ?
                   WHERE user_id = ? AND marketplace = 'wildberries'
                     AND marketplace_product_id = ?""",
                (
                    state.value,
                    format_decimal(margin),
                    format_decimal(price),
                    user_id,
                    nm_id,
                ),
            )

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> StoredProduct:
        raw_state = row["last_margin_state"]
        state: MarginState | None = None
        if raw_state in (MarginState.OK.value, MarginState.BELOW_THRESHOLD.value):
            state = MarginState(str(raw_state))
        cost = _parse_safe_decimal(row["cost_price"])
        if cost is not None and cost < ZERO:
            cost = None
        return StoredProduct(
            nm_id=str(row["marketplace_product_id"]),
            vendor_code=(
                str(row["vendor_code"]) if row["vendor_code"] is not None else None
            ),
            title=str(row["title"]) if row["title"] is not None else None,
            sizes=_deserialize_sizes(row["product_data_json"]),
            cost_price=cost,
            expense_overrides=_deserialize_expenses(row["expense_overrides_json"]),
            last_margin_state=state,
            last_margin=_parse_safe_decimal(row["last_margin"]),
            last_price=_parse_safe_decimal(row["last_price"]),
        )
