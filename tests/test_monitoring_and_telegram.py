"""Integration tests for Monitoring Use Cases, Fault Isolation, and Telegram Handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from cryptography.fernet import Fernet

from margin_guard.application.use_cases.monitoring import RunMonitoringCycleUseCase
from margin_guard.application.use_cases.seller_management import SellerUseCases
from margin_guard.bootstrap import configure_logging, run_application
from margin_guard.config import Settings
from margin_guard.domain.entities import CatalogProduct, MarginAlertItem, ProductSizePrice
from margin_guard.domain.exceptions import (
    MarketplaceAuthError,
    MarketplacePermissionError,
    MarketplaceUnavailableError,
)
from margin_guard.domain.value_objects import ExpenseCategory, ExpenseRule
from margin_guard.infrastructure.persistence.sqlite_db import SqliteDatabase
from margin_guard.infrastructure.persistence.sqlite_repositories import (
    SqliteProductRepository,
    SqliteUserRepository,
)
from margin_guard.infrastructure.security.rate_limiter import SlidingWindowRateLimiter
from margin_guard.infrastructure.security.token_cipher import FernetTokenCipher
from margin_guard.presentation.telegram.formatters import format_margin_alert_summary
from margin_guard.presentation.telegram.handlers import build_telegram_router
from margin_guard.presentation.telegram.notifier import TelegramAlertNotifier


class StubGateway:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def fetch_catalog_products(self, token: str) -> Sequence[CatalogProduct]:
        return self._fn(token)  # type: ignore[no-any-return]


def _catalog_at_price(price: str) -> list[CatalogProduct]:
    dec = Decimal(price)
    return [
        CatalogProduct(
            nm_id="101",
            vendor_code="SKU-101",
            title="Product one",
            sizes=(ProductSizePrice(price=dec, discounted_price=dec, discount_percent=10),),
        ),
        CatalogProduct(
            nm_id="202",
            vendor_code="SKU-202",
            title="Product two",
            sizes=(ProductSizePrice(price=dec, discounted_price=dec, discount_percent=10),),
        ),
    ]


def _handler_callback(router: Router, name: str) -> Any:
    for observer in (router.message, router.edited_message):
        for handler in observer.handlers:
            if handler.callback.__name__ == name:
                return handler.callback
    raise LookupError(f"Handler {name} not found")


def _fake_message(
    text: str = "", *, user_id: int = 123, chat_type: str = "private"
) -> Any:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


def _wire_env(
    tmp_path: Path,
    *,
    gateway_fn: Any = None,
    allowed_ids: set[int] | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
) -> tuple[
    SqliteUserRepository,
    SqliteProductRepository,
    FernetTokenCipher,
    SellerUseCases,
    Router,
]:
    db = SqliteDatabase(tmp_path / "bot.sqlite3")
    db.initialize()
    users = SqliteUserRepository(db)
    products = SqliteProductRepository(db)
    cipher = FernetTokenCipher(Fernet.generate_key())
    gateway = StubGateway(gateway_fn or (lambda _tok: _catalog_at_price("150")))
    use_cases = SellerUseCases.build(
        users=users, products=products, cipher=cipher, gateway=gateway
    )
    router = build_telegram_router(
        use_cases,
        rate_limiter=rate_limiter,
        allowed_telegram_ids=allowed_ids,
    )
    return users, products, cipher, use_cases, router


def test_monitoring_cycle_groups_alerts_retries_delivery_and_isolates_faults(
    tmp_path: Path,
) -> None:
    users, products, cipher, _, _ = _wire_env(tmp_path)

    # Seller 7001: healthy -> drops below threshold
    s1 = users.get_or_create(7001)
    users.save_encrypted_token(7001, cipher.encrypt("tok-7001", subject="7001"))
    users.set_threshold(7001, Decimal("25"))
    users.update_default_expenses(
        7001,
        {
            ExpenseCategory.COMMISSION: ExpenseRule(percent=Decimal("10")),
            ExpenseCategory.LOGISTICS: ExpenseRule(fixed=Decimal("10")),
            ExpenseCategory.TAX: ExpenseRule(percent=Decimal("6")),
        },
    )
    products.upsert_catalog_products(s1.id, _catalog_at_price("150"))
    products.set_cost_price(s1.id, "101", Decimal("50"))
    products.set_cost_price(s1.id, "202", Decimal("50"))

    # Seller 7002: throws unexpected RuntimeError (must NOT crash Seller 7001!)
    users.get_or_create(7002)
    users.save_encrypted_token(7002, cipher.encrypt("tok-crash", subject="7002"))

    # Seller 7003: invalid WB token (401 -> auto-paused + notified)
    users.get_or_create(7003)
    users.save_encrypted_token(7003, cipher.encrypt("tok-401", subject="7003"))

    cycle_num = 0
    sent_messages: list[tuple[int, str]] = []
    fail_7001_delivery_once = True

    def fetch(token: str) -> Sequence[CatalogProduct]:
        if token == "tok-crash":
            raise RuntimeError("boom inside seller 7002")
        if token == "tok-401":
            raise MarketplaceAuthError("rejected")
        return _catalog_at_price("150" if cycle_num == 1 else "100")

    async def send_telegram(chat_id: int, text: str) -> None:
        nonlocal fail_7001_delivery_once
        if chat_id == 7001 and fail_7001_delivery_once:
            fail_7001_delivery_once = False
            raise RuntimeError("temporary telegram outage")
        sent_messages.append((chat_id, text))

    use_case = RunMonitoringCycleUseCase(
        users=users,
        products=products,
        cipher=cipher,
        gateway=StubGateway(fetch),
        notifier=TelegramAlertNotifier(send_telegram),
    )

    async def run_three_cycles() -> None:
        nonlocal cycle_num
        for step in (1, 2, 3):
            cycle_num = step
            await use_case.execute()

    asyncio.run(run_three_cycles())

    # 7003 was paused on cycle 1
    s3_after = users.get_by_telegram_id(7003)
    assert s3_after is not None
    assert s3_after.monitoring_enabled is False

    # 7001 failed delivery on cycle 2, then succeeded on cycle 3!
    alerts_for_7001 = [msg for uid, msg in sent_messages if uid == 7001]
    assert len(alerts_for_7001) == 1
    summary = alerts_for_7001[0]
    assert "Product one" in summary
    assert "Product two" in summary
    assert "маржа 44.00% → 24.00%" in summary
    assert "tok-7001" not in summary


def test_connect_deletes_message_warns_in_group_and_scrubs_edited_messages(
    tmp_path: Path,
) -> None:
    users, _, cipher, _, router = _wire_env(tmp_path)

    # 1. Group chat leak: deletes message, does NOT save, warns to revoke!
    group_msg = _fake_message("/connect leaked-group-token", user_id=123, chat_type="supergroup")
    asyncio.run(_handler_callback(router, "connect_wb")(group_msg))
    group_msg.delete.assert_awaited_once()
    assert users.get_by_telegram_id(123) is None
    assert "отзовите" in group_msg.answer.await_args.args[0].lower()

    # 2. Edited message leak: deletes edited message and refuses to save!
    edited_msg = _fake_message("/connect edited-secret-token", user_id=123, chat_type="private")
    asyncio.run(_handler_callback(router, "guard_edited_secret_messages")(edited_msg))
    edited_msg.delete.assert_awaited_once()
    assert users.get_by_telegram_id(123) is None

    # 3. Failed delete in private chat: refuses to save!
    undeletable = _fake_message("/connect private-tok", user_id=123)
    undeletable.delete.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=123, text="x"), message="cannot delete"
    )
    asyncio.run(_handler_callback(router, "connect_wb")(undeletable))
    assert users.get_by_telegram_id(123) is None

    # 4. Valid private chat connect: deletes message and encrypts bound to user 123
    valid_msg = _fake_message("/connect valid-readonly-token", user_id=123)
    asyncio.run(_handler_callback(router, "connect_wb")(valid_msg))
    valid_msg.delete.assert_awaited_once()
    saved = users.get_by_telegram_id(123)
    assert saved is not None
    assert saved.encrypted_wb_token is not None
    assert (
        cipher.decrypt(saved.encrypted_wb_token, subject="123")
        == "valid-readonly-token"
    )


def test_full_command_lifecycle_scientific_dos_rejection_auth_and_rate_limit(
    tmp_path: Path,
) -> None:
    limiter = SlidingWindowRateLimiter()
    users, products, _, _, router = _wire_env(
        tmp_path, allowed_ids={123}, rate_limiter=limiter
    )

    # Unauthorized user blocked by allowlist
    intruder = _fake_message("/start", user_id=999)
    asyncio.run(_handler_callback(router, "start")(intruder))
    assert "ограничен" in intruder.answer.await_args.args[0]
    assert users.get_by_telegram_id(999) is None

    # Authorized user lifecycle
    asyncio.run(_handler_callback(router, "start")(_fake_message("/start")))
    asyncio.run(
        _handler_callback(router, "connect_wb")(_fake_message("/connect wb-token-123"))
    )

    prod_msg = _fake_message("/products")
    asyncio.run(_handler_callback(router, "products")(prod_msg))
    assert "Product one" in prod_msg.answer.await_args.args[0]

    # Scientific notation DoS attempt rejected cleanly in /cost and /threshold
    dos_cost = _fake_message("/cost 101 1e99999999")
    asyncio.run(_handler_callback(router, "cost")(dos_cost))
    assert "числом" in dos_cost.answer.await_args.args[0]

    dos_threshold = _fake_message("/threshold 1e99999999")
    asyncio.run(_handler_callback(router, "threshold")(dos_threshold))
    assert "числом" in dos_threshold.answer.await_args.args[0]

    # Valid cost, fees, threshold, status, pause, resume, delete_me
    asyncio.run(_handler_callback(router, "cost")(_fake_message("/cost 101 55,25")))
    asyncio.run(
        _handler_callback(router, "fees")(
            _fake_message("/fees default commission=10% logistics=30 tax=6%")
        )
    )
    asyncio.run(
        _handler_callback(router, "fees")(
            _fake_message("/fees 101 commission=12%")
        )
    )
    asyncio.run(
        _handler_callback(router, "fees")(
            _fake_message("/fees 101 commission=default")
        )
    )
    asyncio.run(_handler_callback(router, "threshold")(_fake_message("/threshold 18.5")))

    status_msg = _fake_message("/status")
    asyncio.run(_handler_callback(router, "status")(status_msg))
    status_reply = status_msg.answer.await_args.args[0]
    assert "Товаров в списке: 2" in status_reply
    assert "18.5%" in status_reply
    assert "Общие расходы настроены" in status_reply

    asyncio.run(_handler_callback(router, "pause")(_fake_message("/pause")))
    assert users.get_by_telegram_id(123).monitoring_enabled is False  # type: ignore[union-attr]

    asyncio.run(_handler_callback(router, "resume")(_fake_message("/resume")))
    assert users.get_by_telegram_id(123).monitoring_enabled is True  # type: ignore[union-attr]

    seller = users.get_by_telegram_id(123)
    assert seller is not None
    assert products.list_by_user(seller.id)[0].cost_price == Decimal("55.25")

    asyncio.run(_handler_callback(router, "delete_me")(_fake_message("/delete_me")))
    assert users.get_by_telegram_id(123) is None


def test_alert_summary_truncates_safely_and_bootstrap_requires_secrets(
    tmp_path: Path,
) -> None:
    many_alerts = [
        MarginAlertItem(
            nm_id=str(1000 + idx),
            title=f"Very long product title number {idx} " * 3,
            sale_price=Decimal("999"),
            previous_margin=Decimal("30.00"),
            current_margin=Decimal("12.50"),
        )
        for idx in range(60)
    ]
    text = format_margin_alert_summary(many_alerts, Decimal("20"), max_length=600)
    assert len(text) <= 600
    assert "список сокращён" in text

    configure_logging()
    missing_secrets = Settings(
        _env_file=None, database_path=tmp_path / "boot.sqlite3"
    )
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        asyncio.run(run_application(missing_secrets))


def test_handler_edge_cases_rate_limit_decryption_errors_and_bootstrap_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10.0
    limiter = SlidingWindowRateLimiter(clock=lambda: now)
    gateway_mode = "ok"

    def dynamic_fetch(_token: str) -> Sequence[CatalogProduct]:
        if gateway_mode == "401":
            raise MarketplaceAuthError("bad")
        if gateway_mode == "403":
            raise MarketplacePermissionError("denied")
        if gateway_mode == "500":
            raise MarketplaceUnavailableError("down")
        if gateway_mode == "empty":
            return []
        return [CatalogProduct(nm_id="777", title="NoPriceItem", sizes=())]

    users, _, _, _, router = _wire_env(
        tmp_path, gateway_fn=dynamic_fetch, rate_limiter=limiter
    )

    # Non-private chat rejected for normal commands; missing from_user ignored
    non_priv = _fake_message("/status", chat_type="group")
    asyncio.run(_handler_callback(router, "status")(non_priv))
    assert "личном чате" in non_priv.answer.await_args.args[0]

    no_user = SimpleNamespace(
        text="/start",
        chat=SimpleNamespace(type="private"),
        from_user=None,
        answer=AsyncMock(),
    )
    asyncio.run(_handler_callback(router, "start")(no_user))
    no_user.answer.assert_not_awaited()

    # Oversized inbound message rejected
    huge_msg = _fake_message("/status " + ("x" * 5000))
    asyncio.run(_handler_callback(router, "status")(huge_msg))
    assert "длину" in huge_msg.answer.await_args.args[0]

    # Help prompts & missing profile branches
    for cmd_name, cmd_text in (
        ("connect_wb", "/connect"),
        ("cost", "/cost 123"),
        ("threshold", "/threshold"),
        ("fees", "/fees default"),
        ("status", "/status"),
        ("pause", "/pause"),
        ("resume", "/resume"),
        ("delete_me", "/delete_me"),
        ("products", "/products"),
    ):
        msg = _fake_message(cmd_text, user_id=404)
        asyncio.run(_handler_callback(router, cmd_name)(msg))
        msg.answer.assert_awaited_once()

    # Register user 500 without token -> resume rejected, connect invalid non-ASCII rejected
    asyncio.run(_handler_callback(router, "start")(_fake_message("/start", user_id=500)))
    res_no_tok = _fake_message("/resume", user_id=500)
    asyncio.run(_handler_callback(router, "resume")(res_no_tok))
    assert "/connect" in res_no_tok.answer.await_args.args[0]

    bad_ascii = _fake_message("/connect кириллица", user_id=500)
    asyncio.run(_handler_callback(router, "connect_wb")(bad_ascii))
    assert "ASCII" in bad_ascii.answer.await_args.args[0]

    # Connect valid token and exercise /products error mappings & rate limit
    asyncio.run(
        _handler_callback(router, "connect_wb")(
            _fake_message("/connect valid-tok", user_id=500)
        )
    )
    for mode in ("empty", "ok", "401", "403"):
        gateway_mode = mode
        p_msg = _fake_message("/products", user_id=500)
        asyncio.run(_handler_callback(router, "products")(p_msg))
        p_msg.answer.assert_awaited_once()

    # 5th /products within 60s hits rate limit!
    throttled = _fake_message("/products", user_id=500)
    asyncio.run(_handler_callback(router, "products")(throttled))
    assert "Слишком много запросов" in throttled.answer.await_args.args[0]

    # Advance clock and test 500 error + corrupted ciphertext in /products and monitoring
    now = 200.0
    gateway_mode = "500"
    err500 = _fake_message("/products", user_id=500)
    asyncio.run(_handler_callback(router, "products")(err500))
    assert "Попробуйте позже" in err500.answer.await_args.args[0]

    users.save_encrypted_token(500, "corrupted-ciphertext")
    corrupt_msg = _fake_message("/products", user_id=500)
    asyncio.run(_handler_callback(router, "products")(corrupt_msg))
    assert "расшифровать" in corrupt_msg.answer.await_args.args[0]

    # Monitoring cycle handles corrupted ciphertext, 403 permission error, and 500 transient error
    users.save_encrypted_token(501, FernetTokenCipher(Fernet.generate_key()).encrypt("x", subject="501"))
    other_cipher = FernetTokenCipher(Fernet.generate_key())
    users.save_encrypted_token(502, other_cipher.encrypt("perm-tok", subject="502"))
    users.save_encrypted_token(503, other_cipher.encrypt("temp-tok", subject="503"))

    def monitor_fetch(tok: str) -> Sequence[CatalogProduct]:
        if tok == "perm-tok":
            raise MarketplacePermissionError("403")
        raise MarketplaceUnavailableError("503")

    cycle = RunMonitoringCycleUseCase(
        users=users,
        products=SqliteProductRepository(SqliteDatabase(tmp_path / "bot.sqlite3")),
        cipher=other_cipher,
        gateway=StubGateway(monitor_fetch),
        notifier=TelegramAlertNotifier(AsyncMock()),
    )
    report = asyncio.run(cycle.execute())
    assert report.paused_sellers >= 2

    # Test full bootstrap wiring with mocked Bot and Dispatcher
    import margin_guard.bootstrap as bootstrap_mod

    fake_dp = SimpleNamespace(
        include_router=lambda _r: None,
        start_polling=AsyncMock(),
    )
    fake_bot = SimpleNamespace(
        send_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(bootstrap_mod, "Dispatcher", lambda: fake_dp)
    monkeypatch.setattr(bootstrap_mod, "Bot", lambda token: fake_bot)

    full_settings = Settings(
        _env_file=None,
        database_path=tmp_path / "wired.sqlite3",
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",  # type: ignore[arg-type]
        token_encryption_key=Fernet.generate_key().decode("ascii"),  # type: ignore[arg-type]
    )
    asyncio.run(run_application(full_settings))
    fake_dp.start_polling.assert_awaited_once()
    fake_bot.session.close.assert_awaited_once()
