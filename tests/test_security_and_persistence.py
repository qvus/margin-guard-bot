"""Security, cryptography, rate-limiting, log-redaction, and SQLite persistence tests."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from margin_guard.config import Settings
from margin_guard.domain.entities import CatalogProduct, ProductSizePrice
from margin_guard.domain.exceptions import (
    InvalidEncryptionKeyError,
    TokenDecryptionError,
)
from margin_guard.domain.value_objects import ExpenseCategory, ExpenseRule
from margin_guard.infrastructure.persistence.sqlite_db import SqliteDatabase
from margin_guard.infrastructure.persistence.sqlite_repositories import (
    SqliteProductRepository,
    SqliteUserRepository,
)
from margin_guard.infrastructure.security.log_redactor import (
    RedactingLogFilter,
    redact_sensitive_text,
)
from margin_guard.infrastructure.security.rate_limiter import SlidingWindowRateLimiter
from margin_guard.infrastructure.security.token_cipher import FernetTokenCipher


def test_cipher_binds_ciphertext_to_subject_blocking_transplant_attack() -> None:
    cipher = FernetTokenCipher(Fernet.generate_key())
    victim_ciphertext = cipher.encrypt("wb-victim-secret-token", subject="1001")

    assert cipher.decrypt(victim_ciphertext, subject="1001") == "wb-victim-secret-token"

    # Cross-row transplant to attacker's user row (subject="2002") MUST fail!
    with pytest.raises(TokenDecryptionError, match="mismatch"):
        cipher.decrypt(victim_ciphertext, subject="2002")


def test_cipher_supports_multifernet_zero_downtime_key_rotation() -> None:
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")

    old_cipher = FernetTokenCipher(old_key)
    legacy_ct = old_cipher.encrypt("seller-secret", subject="777")

    rotated_cipher = FernetTokenCipher(f"{new_key},{old_key}")
    assert rotated_cipher.decrypt(legacy_ct, subject="777") == "seller-secret"

    reencrypted = rotated_cipher.rotate(legacy_ct, subject="777")
    new_only_cipher = FernetTokenCipher(new_key)
    assert new_only_cipher.decrypt(reencrypted, subject="777") == "seller-secret"


def test_cipher_rejects_bad_keys_and_never_leaks_ciphertext_in_errors() -> None:
    with pytest.raises(InvalidEncryptionKeyError):
        FernetTokenCipher("not-a-valid-fernet-key")
    with pytest.raises(InvalidEncryptionKeyError):
        FernetTokenCipher("   ")

    cipher = FernetTokenCipher(Fernet.generate_key())
    with pytest.raises(TokenDecryptionError) as exc_info:
        cipher.decrypt("sensitive-looking-ciphertext", subject="123")
    assert "sensitive-looking-ciphertext" not in str(exc_info.value)


def test_sliding_window_rate_limiter_enforces_quota_and_evicts_lru_keys() -> None:
    now = 100.0
    limiter = SlidingWindowRateLimiter(max_tracked_keys=2, clock=lambda: now)

    assert limiter.check("u:1", max_requests=2, window_seconds=10.0) == (True, 0.0)
    assert limiter.check("u:1", max_requests=2, window_seconds=10.0) == (True, 0.0)
    allowed, retry = limiter.check("u:1", max_requests=2, window_seconds=10.0)
    assert allowed is False
    assert retry == 10.0

    # Advance past window
    now = 111.0
    assert limiter.check("u:1", max_requests=2, window_seconds=10.0) == (True, 0.0)

    # Trigger LRU eviction safely without unbounded growth
    limiter.check("u:2", max_requests=2, window_seconds=10.0)
    limiter.check("u:3", max_requests=2, window_seconds=10.0)
    assert len(limiter._buckets) == 2


def test_log_redactor_scrubs_tokens_jwts_ciphertexts_and_crlf_injection() -> None:
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU"
    fake_tg = "123456789:AAHfiqksKZ8WmR2zP9xL0vN4bM7cQ1wE3rT"
    fake_fernet = Fernet(Fernet.generate_key()).encrypt(b"secret").decode("ascii")

    raw = (
        f"Failed request\r\nFAKE LOG ENTRY Authorization: {fake_jwt} "
        f"bot={fake_tg} ct={fake_fernet}"
    )
    sanitized = redact_sensitive_text(raw)
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    assert fake_jwt not in sanitized
    assert fake_tg not in sanitized
    assert fake_fernet not in sanitized
    assert "[REDACTED" in sanitized

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Token leaked: %s",
        args=(fake_tg,),
        exc_info=None,
    )
    assert RedactingLogFilter().filter(record) is True
    assert fake_tg not in record.getMessage()


def test_sqlite_database_closes_connections_migrates_legacy_schema_and_sets_chmod(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "secure_data" / "app.sqlite3"
    # Seed stage-1 schema
    db_file.parent.mkdir(parents=True)
    raw_conn = sqlite3.connect(db_file)
    try:
        raw_conn.executescript(
            """CREATE TABLE users (
                   id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               );
               CREATE TABLE products (
                   id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                   marketplace TEXT NOT NULL DEFAULT 'wildberries',
                   marketplace_product_id TEXT NOT NULL, cost_price TEXT,
                   UNIQUE (user_id, marketplace, marketplace_product_id)
               );"""
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    db = SqliteDatabase(db_file)
    db.initialize()

    if os.name == "posix":
        dir_mode = stat.S_IMODE(db_file.parent.stat().st_mode)
        file_mode = stat.S_IMODE(db_file.stat().st_mode)
        assert dir_mode == 0o700
        assert file_mode == 0o600

    with db.connection() as active_conn:
        active_conn.execute("SELECT 1")

    # Verify connection descriptor is genuinely closed on context exit!
    with pytest.raises(sqlite3.ProgrammingError):
        active_conn.execute("SELECT 1")

    users = SqliteUserRepository(db)
    products = SqliteProductRepository(db)
    seller = users.get_or_create(555)
    products.upsert_catalog_products(
        seller.id,
        [
            CatalogProduct(
                nm_id="99",
                vendor_code="SKU-99",
                title="Test\nItem",
                sizes=(ProductSizePrice(price=Decimal("100"), discounted_price=Decimal("80")),),
            )
        ],
    )
    products.set_cost_price(seller.id, "99", Decimal("40"))
    products.update_expense_overrides(
        seller.id, "99", {ExpenseCategory.TAX: ExpenseRule(percent=Decimal("6"))}
    )
    stored = products.list_by_user(seller.id)
    assert len(stored) == 1
    assert stored[0].title == "Test Item"
    assert stored[0].cost_price == Decimal("40")
    assert stored[0].sizes[0].discounted_price == Decimal("80")

    assert users.delete_by_telegram_id(555) is True
    assert users.get_by_telegram_id(555) is None


def test_settings_immutability_interval_bounds_and_allowlist_parsing() -> None:
    settings = Settings(
        _env_file=None,
        allowed_telegram_ids="101, 202",  # type: ignore[arg-type]
    )
    assert settings.app_env == "development"
    assert settings.monitoring_interval_minutes == 45
    assert settings.allowed_telegram_ids == frozenset({101, 202})

    with pytest.raises(ValidationError):
        settings.monitoring_interval_minutes = 30  # type: ignore[misc]

    for invalid_interval in (29, 61):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, monitoring_interval_minutes=invalid_interval)
