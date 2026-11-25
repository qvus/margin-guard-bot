import asyncio
import json
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from margin_guard import repository
from margin_guard.database import initialize_database
from margin_guard.monitoring import evaluate_margin_state
from margin_guard.monitoring import calculate_product_margin
from margin_guard.monitoring_service import run_monitoring_cycle
from margin_guard.token_encryption import TokenCipher
from margin_guard.wildberries import WBAuthenticationError


def test_ok_to_below_threshold_sends_alert():
    decision = evaluate_margin_state(Decimal("9.99"), Decimal("10"), "ok")

    assert decision.state == "below_threshold"
    assert decision.changed is True
    assert decision.notify is True


def test_below_threshold_state_does_not_repeat_alert():
    decision = evaluate_margin_state(
        Decimal("5"), Decimal("10"), "below_threshold"
    )

    assert decision.state == "below_threshold"
    assert decision.changed is False
    assert decision.notify is False


def test_recovery_updates_state_without_sending_low_margin_alert():
    decision = evaluate_margin_state(
        Decimal("10"), Decimal("10"), "below_threshold"
    )

    assert decision.state == "ok"
    assert decision.changed is True
    assert decision.notify is False


def test_first_measurement_sets_state_without_alert():
    decision = evaluate_margin_state(Decimal("2"), Decimal("10"), None)

    assert decision.state == "below_threshold"
    assert decision.changed is True
    assert decision.notify is False


def test_missing_margin_leaves_last_known_state_unchanged():
    decision = evaluate_margin_state(None, Decimal("10"), "ok")

    assert decision.state == "ok"
    assert decision.changed is False
    assert decision.notify is False


def test_margin_values_must_be_decimal():
    with pytest.raises(TypeError, match="margin must be Decimal"):
        evaluate_margin_state(1.5, Decimal("10"), "ok")

    with pytest.raises(TypeError, match="threshold must be Decimal"):
        evaluate_margin_state(Decimal("1"), 10, "ok")


def test_product_margin_uses_discounted_prices_and_selects_lowest_size():
    product = {
        "pricing": {
            "sizes": [
                {"price": 150, "discountedPrice": 150},
                {"price": 100, "discountedPrice": 80},
            ]
        }
    }
    product_row = {
        "cost_price": "50",
        "expense_overrides_json": "{}",
    }
    expenses = {
        "commission": {"fixed": "0", "percent": "10"},
        "logistics": {"fixed": "5", "percent": "0"},
        "tax": {"fixed": "0", "percent": "5"},
    }

    margin, sale_price = calculate_product_margin(
        product, product_row, json.dumps(expenses)
    )

    assert sale_price == Decimal("80")
    assert margin == Decimal("16.25")


def test_product_margin_is_unknown_when_required_expense_is_missing():
    product = {"pricing": {"sizes": [{"discountedPrice": 100}]}}
    row = {"cost_price": "50", "expense_overrides_json": "{}"}
    expenses = {"commission": {"fixed": "0", "percent": "10"}}

    assert calculate_product_margin(product, row, json.dumps(expenses)) is None


def test_product_expense_overrides_replace_user_defaults():
    product = {"pricing": {"sizes": [{"discountedPrice": 100}]}}
    row = {
        "cost_price": "50",
        "expense_overrides_json": json.dumps(
            {"logistics": {"fixed": "20", "percent": "0"}}
        ),
    }
    defaults = {
        "commission": {"fixed": "0", "percent": "10"},
        "logistics": {"fixed": "5", "percent": "0"},
        "tax": {"fixed": "0", "percent": "5"},
    }

    margin, price = calculate_product_margin(product, row, json.dumps(defaults))

    assert price == Decimal("100")
    assert margin == Decimal("15.00")


def _setup_monitoring_user(database_path, cipher):
    initialize_database(database_path)
    user = repository.get_or_create_user(database_path, 7001)
    repository.save_encrypted_token(database_path, 7001, cipher.encrypt("private-wb-token"))
    repository.set_threshold(database_path, 7001, "25")
    repository.set_expenses(
        database_path,
        7001,
        {
            "commission": {"fixed": "0", "percent": "10"},
            "logistics": {"fixed": "10", "percent": "0"},
            "tax": {"fixed": "0", "percent": "6"},
        },
    )
    return user


def _products_at_price(price):
    return [
        {
            "card": {"nmID": 101, "title": "Product one"},
            "pricing": {"sizes": [{"discountedPrice": price}]},
        },
        {
            "card": {"nmID": 202, "title": "Product two"},
            "pricing": {"sizes": [{"discountedPrice": price}]},
        },
    ]


def test_cycle_sends_one_grouped_notification_only_on_ok_to_below_transition(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    cipher = TokenCipher(Fernet.generate_key())
    user = _setup_monitoring_user(db, cipher)
    repository.save_products(db, user["id"], _products_at_price(150))
    repository.set_product_cost(db, user["id"], "101", "50")
    repository.set_product_cost(db, user["id"], "202", "50")
    fetch_count = 0
    sent = []

    def fetch(_token):
        nonlocal fetch_count
        fetch_count += 1
        return _products_at_price(150 if fetch_count == 1 else 100)

    async def send(telegram_id, text):
        sent.append((telegram_id, text))

    async def run_three_cycles():
        for _ in range(3):
            await run_monitoring_cycle(db, cipher, send, fetch_products=fetch)

    asyncio.run(run_three_cycles())

    assert len(sent) == 1
    chat_id, summary = sent[0]
    assert chat_id == 7001
    assert "Product one" in summary and "Product two" in summary
    assert "цена 100" in summary
    assert "маржа 44.00% → 24.00%" in summary
    assert "порог 25%" in summary
    assert "private-wb-token" not in summary


def test_invalid_wb_token_pauses_user_until_reconnected(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    cipher = TokenCipher(Fernet.generate_key())
    _setup_monitoring_user(db, cipher)
    sent = []

    async def send(telegram_id, text):
        sent.append((telegram_id, text))

    def fetch(_token):
        raise WBAuthenticationError("sanitized failure")

    asyncio.run(run_monitoring_cycle(db, cipher, send, fetch_products=fetch))

    row = repository.get_user(db, 7001)
    assert row["monitoring_enabled"] == 0
    assert len(sent) == 1
    assert "приостановлены" in sent[0][1]
    assert "private-wb-token" not in sent[0][1]

    repository.save_encrypted_token(db, 7001, cipher.encrypt("new-token"))
    assert repository.get_user(db, 7001)["monitoring_enabled"] == 1


def test_unknown_margin_does_not_create_or_change_a_saved_state(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    cipher = TokenCipher(Fernet.generate_key())
    user = _setup_monitoring_user(db, cipher)
    repository.save_products(db, user["id"], _products_at_price(100))
    sent = []

    async def send(telegram_id, text):
        sent.append((telegram_id, text))

    asyncio.run(
        run_monitoring_cycle(
            db,
            cipher,
            send,
            fetch_products=lambda _token: _products_at_price(100),
        )
    )

    rows = repository.list_products(db, user["id"])
    assert all(row["last_margin_state"] is None for row in rows)
    assert sent == []


def test_failed_telegram_delivery_retries_the_state_transition_next_cycle(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    cipher = TokenCipher(Fernet.generate_key())
    user = _setup_monitoring_user(db, cipher)
    repository.save_products(db, user["id"], _products_at_price(150))
    repository.set_product_cost(db, user["id"], "101", "50")
    repository.set_product_cost(db, user["id"], "202", "50")
    fetch_count = 0
    delivery_attempts = []

    def fetch(_token):
        nonlocal fetch_count
        fetch_count += 1
        return _products_at_price(150 if fetch_count == 1 else 100)

    async def send(telegram_id, text):
        delivery_attempts.append((telegram_id, text))
        if len(delivery_attempts) == 1:
            raise RuntimeError("temporary delivery failure")

    async def run_cycles():
        for _ in range(3):
            await run_monitoring_cycle(db, cipher, send, fetch_products=fetch)

    asyncio.run(run_cycles())

    assert len(delivery_attempts) == 2
    assert "Product one" in delivery_attempts[1][1]
    rows = repository.list_products(db, user["id"])
    assert all(row["last_margin_state"] == "below_threshold" for row in rows)
