"""Initialize the application and start the Telegram bot."""

import asyncio

from aiogram import Bot, Dispatcher

from .config import get_settings
from .database import initialize_database
from .telegram_bot import build_router
from .token_encryption import TokenCipher


async def _run_bot(bot: Bot, dispatcher: Dispatcher) -> None:
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    settings = get_settings()
    initialize_database(settings.database_path)
    print(f"База данных готова: {settings.database_path}")
    if settings.telegram_bot_token is None or settings.token_encryption_key is None:
        raise SystemExit(
            "Для запуска бота укажите TELEGRAM_BOT_TOKEN и TOKEN_ENCRYPTION_KEY "
            "в локальном файле .env. Пример: .env.example"
        )

    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings.database_path, cipher))
    try:
        asyncio.run(_run_bot(bot, dispatcher))
    except KeyboardInterrupt:
        print("Бот остановлен.")


if __name__ == "__main__":
    main()
