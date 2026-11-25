import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from margin_guard import repository
from margin_guard.database import initialize_database
from margin_guard.telegram_bot import build_router
from margin_guard.token_encryption import TokenCipher, TokenEncryptionError


def _callback(router, name):
    return next(
        handler.callback
        for handler in router.message.handlers
        if handler.callback.__name__ == name
    )


def _message(text="", *, user_id=123, chat_type="private"):
    from types import SimpleNamespace

    message = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )
    return message


def test_token_cipher_encrypts_and_decrypts_and_rejects_wrong_key():
    cipher = TokenCipher(Fernet.generate_key())
    encrypted = cipher.encrypt("wb-secret-value")
    assert encrypted != "wb-secret-value"
    assert "wb-secret-value" not in encrypted
    assert cipher.decrypt(encrypted) == "wb-secret-value"

    with pytest.raises(TokenEncryptionError, match="invalid"):
        TokenCipher("not-a-fernet-key")


def test_token_cipher_reports_invalid_ciphertext_without_exposing_it():
    cipher = TokenCipher(Fernet.generate_key())
    with pytest.raises(TokenEncryptionError, match="could not be decrypted") as error:
        cipher.decrypt("private-ciphertext")
    assert "private-ciphertext" not in str(error.value)


def test_database_migrates_stage_one_schema_and_cascades_user_deletion(tmp_path):
    db = tmp_path / "old.sqlite3"
    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.executescript(
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
    initialize_database(db)
    user = repository.get_or_create_user(db, 123)
    repository.save_products(db, user["id"], [{"card": {"nmID": 9, "title": "Sample"}, "pricing": None}])
    assert len(repository.list_products(db, user["id"])) == 1
    assert repository.delete_user(db, 123)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_start_creates_user_and_explains_read_only_token(tmp_path):
    initialize_database(tmp_path / "bot.sqlite3")
    router = build_router(tmp_path / "bot.sqlite3", TokenCipher(Fernet.generate_key()))
    message = _message("/start")

    asyncio.run(_callback(router, "start")(message))

    assert repository.get_user(tmp_path / "bot.sqlite3", 123) is not None
    reply = message.answer.await_args.args[0]
    assert "только на чтение" in reply
    assert "не умею менять" in reply


def test_connect_deletes_command_and_saves_only_encrypted_token(tmp_path):
    db = tmp_path / "bot.sqlite3"
    initialize_database(db)
    cipher = TokenCipher(Fernet.generate_key())
    router = build_router(db, cipher)
    secret = "wb-private-token"
    message = _message(f"/connect {secret}")

    asyncio.run(_callback(router, "connect_wb")(message))

    message.delete.assert_awaited_once()
    row = repository.get_user(db, 123)
    assert row is not None
    assert row["wb_token_encrypted"] != secret
    assert cipher.decrypt(row["wb_token_encrypted"]) == secret
    assert secret not in message.answer.await_args.args[0]


def test_connect_does_not_save_token_if_telegram_cannot_delete_message(tmp_path):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    db = tmp_path / "bot.sqlite3"
    initialize_database(db)
    router = build_router(db, TokenCipher(Fernet.generate_key()))
    message = _message("/connect wb-private-token")
    message.delete.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=123, text="test"), message="can't delete"
    )

    asyncio.run(_callback(router, "connect_wb")(message))

    assert repository.get_user(db, 123) is None
    assert "не сохранил" in message.answer.await_args.args[0]


def test_products_fetches_and_caches_products_without_exposing_token(tmp_path, monkeypatch):
    import margin_guard.telegram_bot as telegram_bot

    db = tmp_path / "bot.sqlite3"
    initialize_database(db)
    cipher = TokenCipher(Fernet.generate_key())
    repository.save_encrypted_token(db, 123, cipher.encrypt("wb-secret"))
    monkeypatch.setattr(
        telegram_bot,
        "_fetch_products",
        lambda token: [
            {
                "card": {"nmID": 100, "vendorCode": "SKU-100", "title": "Sample item"},
                "pricing": {"sizes": [{"price": 1000, "discountedPrice": 800, "discount": 20}]},
            }
        ] if token == "wb-secret" else pytest.fail("wrong token"),
    )
    router = build_router(db, cipher)
    message = _message("/products")

    asyncio.run(_callback(router, "products")(message))

    user = repository.get_user(db, 123)
    saved = repository.list_products(db, user["id"])
    assert len(saved) == 1
    assert saved[0]["marketplace_product_id"] == "100"
    reply = message.answer.await_args.args[0]
    assert "Sample item" in reply
    assert "после скидки 800" in reply
    assert "wb-secret" not in reply


def test_cost_threshold_status_and_delete_me_commands(tmp_path):
    db = tmp_path / "bot.sqlite3"
    initialize_database(db)
    router = build_router(db, TokenCipher(Fernet.generate_key()))
    user = repository.get_or_create_user(db, 123)
    repository.save_products(db, user["id"], [{"card": {"nmID": 456, "title": "Item"}}])

    cost_message = _message("/cost 456 125,50")
    asyncio.run(_callback(router, "cost")(cost_message))
    assert Decimal(repository.list_products(db, user["id"])[0]["cost_price"]) == Decimal("125.50")

    threshold_message = _message("/threshold 17.5")
    asyncio.run(_callback(router, "threshold")(threshold_message))
    assert repository.get_user(db, 123)["margin_threshold"] == "17.5"

    status_message = _message("/status")
    asyncio.run(_callback(router, "status")(status_message))
    status_text = status_message.answer.await_args.args[0]
    assert "Товаров в списке: 1" in status_text
    assert "17.5%" in status_text

    delete_message = _message("/delete_me")
    asyncio.run(_callback(router, "delete_me")(delete_message))
    assert repository.get_user(db, 123) is None
