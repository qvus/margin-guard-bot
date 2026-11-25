"""Composition root: wire Clean Architecture adapters, logging, scheduler, and polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from margin_guard.application.use_cases.monitoring import RunMonitoringCycleUseCase
from margin_guard.application.use_cases.seller_management import SellerUseCases
from margin_guard.config import Settings, get_settings
from margin_guard.infrastructure.marketplace.wildberries_client import WildberriesGateway
from margin_guard.infrastructure.persistence.sqlite_db import SqliteDatabase
from margin_guard.infrastructure.persistence.sqlite_repositories import (
    SqliteProductRepository,
    SqliteUserRepository,
)
from margin_guard.infrastructure.security.log_redactor import RedactingLogFilter
from margin_guard.infrastructure.security.rate_limiter import SlidingWindowRateLimiter
from margin_guard.infrastructure.security.token_cipher import FernetTokenCipher
from margin_guard.presentation.telegram.handlers import build_telegram_router
from margin_guard.presentation.telegram.notifier import TelegramAlertNotifier

logger = logging.getLogger("margin_guard")


def configure_logging() -> None:
    """Configure root logging with mandatory credential/CRLF redaction on every handler."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redactor = RedactingLogFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redactor)
    for handler in root_logger.handlers:
        handler.addFilter(redactor)

    for noisy_logger in ("aiogram", "httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


async def run_application(settings: Settings) -> None:
    """Assemble adapters, run scheduled monitoring, and poll Telegram updates."""
    database = SqliteDatabase(settings.database_path)
    database.initialize()
    logger.info("SQLite storage initialized at %s", settings.database_path)

    if settings.telegram_bot_token is None or settings.token_encryption_key is None:
        raise SystemExit(
            "Для запуска бота укажите TELEGRAM_BOT_TOKEN и TOKEN_ENCRYPTION_KEY "
            "в локальном файле .env. Пример: .env.example"
        )

    cipher = FernetTokenCipher(settings.token_encryption_key.get_secret_value())
    user_repo = SqliteUserRepository(database)
    product_repo = SqliteProductRepository(database)
    wb_gateway = WildberriesGateway(
        timeout=settings.wb_request_timeout_seconds,
        max_retries=settings.wb_max_retries,
    )
    rate_limiter = SlidingWindowRateLimiter()
    seller_use_cases = SellerUseCases.build(
        users=user_repo,
        products=product_repo,
        cipher=cipher,
        gateway=wb_gateway,
    )

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    notifier = TelegramAlertNotifier(bot.send_message)
    monitoring_use_case = RunMonitoringCycleUseCase(
        users=user_repo,
        products=product_repo,
        cipher=cipher,
        gateway=wb_gateway,
        notifier=notifier,
        max_concurrent_sellers=settings.monitoring_max_concurrency,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_telegram_router(
            seller_use_cases,
            rate_limiter=rate_limiter,
            allowed_telegram_ids=settings.allowed_telegram_ids,
        )
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        monitoring_use_case.execute,
        "interval",
        minutes=settings.monitoring_interval_minutes,
        id="margin-monitoring",
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "Starting margin monitoring every %d minutes",
        settings.monitoring_interval_minutes,
    )
    scheduler.start()
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "edited_message"],
            handle_signals=True,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        logger.info("Margin Guard bot shut down cleanly")


def main() -> None:
    """CLI entrypoint."""
    configure_logging()
    settings = get_settings()
    try:
        asyncio.run(run_application(settings))
    except KeyboardInterrupt:
        logger.info("Bot stopped via keyboard interrupt")
