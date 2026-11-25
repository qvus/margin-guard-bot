"""Thin Aiogram command handlers delegating strictly to Application Use Cases."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from contextlib import suppress

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from margin_guard.application.ports import RateLimiterPort
from margin_guard.application.use_cases.seller_management import SellerUseCases
from margin_guard.domain.exceptions import (
    DomainValidationError,
    EntityNotFoundError,
    MarketplaceAuthError,
    MarketplaceError,
    MarketplacePermissionError,
    SecretProtectionError,
)
from margin_guard.domain.value_objects import format_decimal
from margin_guard.presentation.telegram.formatters import (
    format_product_catalog_messages,
    format_seller_status,
)

MAX_INBOUND_MESSAGE_LENGTH = 4200
GENERAL_RATE_LIMIT = (20, 60.0)
SYNC_PRODUCTS_RATE_LIMIT = (4, 60.0)
CONNECT_RATE_LIMIT = (5, 60.0)


def _command_args(message: Message) -> list[str]:
    text = message.text or ""
    if len(text) > MAX_INBOUND_MESSAGE_LENGTH:
        return []
    parts = text.split()
    return parts[1:] if parts else []


def _extract_connect_token_argument(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def build_telegram_router(
    use_cases: SellerUseCases,
    *,
    rate_limiter: RateLimiterPort | None = None,
    allowed_telegram_ids: AbstractSet[int] | None = None,
) -> Router:
    """Build a hardened Aiogram Router wired to seller application use cases."""
    router = Router(name="margin-guard-commands")
    allowlist = frozenset(allowed_telegram_ids) if allowed_telegram_ids else frozenset()

    async def authorize_private_actor(
        message: Message,
        *,
        bucket: str = "cmd",
        limit: tuple[int, float] = GENERAL_RATE_LIMIT,
    ) -> int | None:
        if message.chat.type != "private":
            await message.answer("Пожалуйста, используйте бота в личном чате.")
            return None
        user = message.from_user
        if user is None:
            return None
        if allowlist and user.id not in allowlist:
            await message.answer("Доступ к этому экземпляру бота ограничен администратором.")
            return None
        raw_text = message.text or ""
        if len(raw_text) > MAX_INBOUND_MESSAGE_LENGTH:
            await message.answer("Сообщение превышает допустимую длину.")
            return None
        if rate_limiter is not None:
            allowed, retry_after = rate_limiter.check(
                f"{bucket}:{user.id}",
                max_requests=limit[0],
                window_seconds=limit[1],
            )
            if not allowed:
                await message.answer(
                    f"Слишком много запросов. Повторите попытку через {retry_after:.0f} сек."
                )
                return None
        return int(user.id)

    @router.edited_message()
    async def guard_edited_secret_messages(message: Message) -> None:
        """Delete edited messages containing /connect credentials immediately."""
        text = (message.text or "").lstrip()
        if not text.startswith("/connect"):
            return
        token = _extract_connect_token_argument(text)
        if not token:
            return
        with suppress(TelegramAPIError):
            await message.delete()
        await message.answer(
            "Редактирование сообщений с токеном небезопасно: токен не сохранён. "
            "Отправьте новую команду /connect <токен> в личном чате."
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        await use_cases.register.execute(actor_id)
        await message.answer(
            "Привет! Я помогу отслеживать товары и их маржу на Wildberries.\n\n"
            "Подключите WB API токен только на чтение командой /connect. "
            "Я не умею менять цены, скидки, карточки или остатки.\n\n"
            "Команды: /connect, /products, /cost, /fees, /threshold, /status, "
            "/pause, /resume, /delete_me."
        )

    @router.message(Command("connect"))
    async def connect_wb(message: Message) -> None:
        text = message.text or ""
        token = _extract_connect_token_argument(text)
        deleted_ok = False
        if token:
            try:
                await message.delete()
                deleted_ok = True
            except TelegramAPIError:
                deleted_ok = False

        if message.chat.type != "private":
            if token:
                await message.answer(
                    "⚠️ Внимание: вы отправили токен в групповом чате! Токен НЕ сохранён. "
                    "Немедленно отзовите этот токен в личном кабинете Wildberries и выпустите новый, "
                    "затем подключите его в личном чате с ботом."
                )
            else:
                await message.answer("Пожалуйста, используйте бота в личном чате.")
            return

        if token and not deleted_ok:
            await message.answer(
                "Не удалось удалить сообщение с токеном, поэтому я не сохранил его. "
                "Отправьте /connect <токен> ещё раз в личном чате."
            )
            return

        actor_id = await authorize_private_actor(
            message, bucket="connect", limit=CONNECT_RATE_LIMIT
        )
        if actor_id is None:
            return

        if not token or any(ch.isspace() for ch in token):
            await message.answer(
                "Отправьте команду в личном чате в формате:\n"
                "/connect <WB API токен только на чтение>\n\n"
                "После обработки сообщение с командой будет удалено."
            )
            return

        try:
            await use_cases.connect_token.execute(actor_id, token)
        except DomainValidationError as exc:
            await message.answer(str(exc))
            return
        except SecretProtectionError:
            await message.answer(
                "Не удалось безопасно сохранить токен. Проверьте настройки шифрования."
            )
            return

        await message.answer(
            "Токен сохранён в зашифрованном виде. Для чтения товаров также нужны права "
            "только на чтение для категорий Контент и Цены и скидки. "
            "Команда /products загрузит карточки и цены."
        )

    @router.message(Command("products"))
    async def products(message: Message) -> None:
        actor_id = await authorize_private_actor(
            message, bucket="products", limit=SYNC_PRODUCTS_RATE_LIMIT
        )
        if actor_id is None:
            return
        try:
            fetched = await use_cases.sync_products.execute(actor_id)
        except EntityNotFoundError as exc:
            await message.answer(str(exc))
            return
        except SecretProtectionError:
            await message.answer(
                "Не удалось расшифровать сохранённый токен. Подключите его повторно через /connect."
            )
            return
        except MarketplaceAuthError:
            await message.answer(
                "Wildberries отклонил токен. Проверьте, что он действителен и имеет права "
                "только на чтение для категорий Контент и Цены и скидки."
            )
            return
        except MarketplacePermissionError:
            await message.answer(
                "У токена WB недостаточно прав чтения. Включите чтение для разделов "
                "Контент и Цены и скидки."
            )
            return
        except MarketplaceError:
            await message.answer(
                "Не удалось получить товары из WB. Попробуйте позже или проверьте права токена."
            )
            return

        if not fetched:
            await message.answer("WB не вернул карточки товаров.")
            return
        for chunk in format_product_catalog_messages(fetched):
            await message.answer(chunk)

    @router.message(Command("cost"))
    async def cost(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        args = _command_args(message)
        if len(args) != 2:
            await message.answer(
                "Формат: /cost <артикул WB> <себестоимость>. Например: /cost 123456789 450.50"
            )
            return
        try:
            await use_cases.set_cost.execute(actor_id, args[0], args[1])
        except (DomainValidationError, EntityNotFoundError) as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Себестоимость для товара {args[0].strip()} сохранена.")

    @router.message(Command("threshold"))
    async def threshold(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        args = _command_args(message)
        if len(args) != 1:
            await message.answer("Формат: /threshold <процент>. Например: /threshold 15")
            return
        try:
            saved_value = await use_cases.set_threshold.execute(actor_id, args[0])
        except DomainValidationError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Порог маржи установлен: {format_decimal(saved_value)}%.")

    @router.message(Command("fees"))
    async def fees(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        args = _command_args(message)
        if len(args) < 2:
            await message.answer(
                "Укажите default для значений по умолчанию или артикул WB. "
                "Формат: /fees default commission=10% logistics=50 tax=6%\n"
                "Значение можно задать суммой (50), процентом (6%) или обоими "
                "(50+6%). Хранение и реклама необязательны.\n"
                "Для товара: /fees 123456789 commission=12%. Чтобы вернуть "
                "категорию товара к значению по умолчанию: commission=default."
            )
            return
        scope, *assignments = args
        try:
            resolved_scope = await use_cases.configure_expenses.execute(
                actor_id, scope, assignments
            )
        except (DomainValidationError, EntityNotFoundError) as exc:
            await message.answer(str(exc))
            return

        if resolved_scope == "default":
            await message.answer("Расходы по умолчанию сохранены.")
        else:
            await message.answer(f"Расходы для товара {resolved_scope} сохранены.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        try:
            dto = await use_cases.get_status.execute(actor_id)
        except EntityNotFoundError as exc:
            await message.answer(str(exc))
            return
        await message.answer(format_seller_status(dto))

    @router.message(Command("pause"))
    async def pause(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        changed = await use_cases.pause_monitoring.execute(actor_id)
        await message.answer(
            "Проверки приостановлены." if changed else "Сначала выполните /start."
        )

    @router.message(Command("resume"))
    async def resume(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        try:
            await use_cases.resume_monitoring.execute(actor_id)
        except (DomainValidationError, EntityNotFoundError) as exc:
            await message.answer(str(exc))
            return
        await message.answer("Проверки возобновлены.")

    @router.message(Command("delete_me"))
    async def delete_me(message: Message) -> None:
        actor_id = await authorize_private_actor(message)
        if actor_id is None:
            return
        removed = await use_cases.delete_account.execute(actor_id)
        if removed:
            await message.answer("Ваш токен и данные удалены.")
        else:
            await message.answer("Профиль уже отсутствует.")

    return router
