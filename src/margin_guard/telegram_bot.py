"""Telegram command handlers; storage and WB access stay in their own modules."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from . import repository
from .token_encryption import TokenCipher, TokenEncryptionError
from .wildberries import WBApiError, WBAuthenticationError, WildberriesClient

MAX_TELEGRAM_MESSAGE = 3500


def build_router(database_path: str | Path, cipher: TokenCipher) -> Router:
    """Create command handlers bound to this bot's database and token cipher."""
    router = Router(name="margin-guard-commands")

    async def require_private_chat(message: Message) -> bool:
        if message.chat.type != "private":
            await message.answer("Пожалуйста, используйте бота в личном чате.")
            return False
        if message.from_user is None:
            return False
        return True

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        repository.get_or_create_user(database_path, message.from_user.id)
        await message.answer(
            "Привет! Я помогу отслеживать товары и их маржу на Wildberries.\n\n"
            "Подключите WB API токен только на чтение командой /connect. "
            "Я не умею менять цены, скидки, карточки или остатки.\n\n"
            "Команды: /connect, /products, /cost, /threshold, /status, /delete_me."
        )

    @router.message(Command("connect"))
    async def connect_wb(message: Message) -> None:
        text = message.text or ""
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        # The command contains a credential. Remove it from the chat as soon as
        # possible; never echo it in a response or write it to logs.
        if token:
            try:
                await message.delete()
            except TelegramAPIError:
                await message.answer(
                    "Не удалось удалить сообщение с токеном, поэтому я не сохранил его. "
                    "Отправьте /connect <токен> ещё раз в личном чате."
                )
                return
        if not await require_private_chat(message):
            return
        if not token or any(char.isspace() for char in token):
            await message.answer(
                "Отправьте команду в личном чате в формате:\n"
                "/connect <WB API токен только на чтение>\n\n"
                "После обработки сообщение с командой будет удалено."
            )
            return
        assert message.from_user is not None
        try:
            encrypted = cipher.encrypt(token)
            repository.save_encrypted_token(database_path, message.from_user.id, encrypted)
        except TokenEncryptionError:
            await message.answer("Не удалось безопасно сохранить токен. Проверьте настройки шифрования.")
            return
        await message.answer(
            "Токен сохранён в зашифрованном виде. Для чтения товаров также нужны права "
            "только на чтение для категорий Контент и Цены и скидки. "
            "Команда /products загрузит карточки и цены."
        )

    @router.message(Command("products"))
    async def products(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        user = repository.get_user(database_path, message.from_user.id)
        if user is None or not user["wb_token_encrypted"]:
            await message.answer("Сначала подключите WB API токен командой /connect.")
            return
        try:
            token = cipher.decrypt(user["wb_token_encrypted"])
        except TokenEncryptionError:
            await message.answer("Не удалось расшифровать сохранённый токен. Подключите его повторно через /connect.")
            return

        try:
            fetched = await asyncio.to_thread(_fetch_products, token)
        except WBAuthenticationError:
            await message.answer(
                "Wildberries отклонил токен. Проверьте, что он действителен и имеет права "
                "только на чтение для категорий Контент и Цены и скидки."
            )
            return
        except WBApiError:
            await message.answer("Не удалось получить товары из WB. Попробуйте позже или проверьте права токена.")
            return

        repository.save_products(database_path, user["id"], fetched)
        if not fetched:
            await message.answer("WB не вернул карточки товаров.")
            return
        for chunk in _format_product_messages(fetched):
            await message.answer(chunk)

    @router.message(Command("cost"))
    async def cost(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        args = _command_args(message)
        if len(args) != 2:
            await message.answer("Формат: /cost <артикул WB> <себестоимость>. Например: /cost 123456789 450.50")
            return
        try:
            value = Decimal(args[1].replace(",", "."))
        except InvalidOperation:
            await message.answer("Себестоимость должна быть числом, например: /cost 123456789 450.50")
            return
        if not value.is_finite() or value < 0:
            await message.answer("Себестоимость должна быть конечным числом от нуля и выше.")
            return
        user = repository.get_user(database_path, message.from_user.id)
        if user is None:
            await message.answer("Сначала выполните /start, подключите WB токен и загрузите товары через /products.")
            return
        saved = repository.set_product_cost(
            database_path, user["id"], args[0], format(value, "f")
        )
        if not saved:
            await message.answer("Товар не найден. Сначала загрузите список через /products и проверьте артикул WB.")
            return
        await message.answer(f"Себестоимость для товара {args[0]} сохранена.")

    @router.message(Command("threshold"))
    async def threshold(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        args = _command_args(message)
        if len(args) != 1:
            await message.answer("Формат: /threshold <процент>. Например: /threshold 15")
            return
        try:
            value = Decimal(args[0].replace(",", "."))
        except InvalidOperation:
            await message.answer("Порог должен быть числом, например: /threshold 15")
            return
        if not value.is_finite():
            await message.answer("Порог должен быть конечным числом.")
            return
        repository.set_threshold(database_path, message.from_user.id, format(value, "f"))
        await message.answer(f"Порог маржи установлен: {format(value, 'f')}%.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        user = repository.get_user(database_path, message.from_user.id)
        if user is None:
            await message.answer("Профиль ещё не создан. Выполните /start.")
            return
        count = len(repository.list_products(database_path, user["id"]))
        connected = "подключён" if user["wb_token_encrypted"] else "не подключён"
        await message.answer(
            f"WB токен: {connected}\n"
            f"Товаров в списке: {count}\n"
            f"Порог маржи: {user['margin_threshold']}%"
        )

    @router.message(Command("delete_me"))
    async def delete_me(message: Message) -> None:
        if not await require_private_chat(message):
            return
        assert message.from_user is not None
        removed = repository.delete_user(database_path, message.from_user.id)
        if removed:
            await message.answer("Ваш токен и данные удалены.")
        else:
            await message.answer("Профиль уже отсутствует.")

    return router


def _command_args(message: Message) -> list[str]:
    parts = (message.text or "").split()
    return parts[1:] if parts else []


def _fetch_products(token: str) -> list[dict[str, Any]]:
    with WildberriesClient(token) as client:
        return client.list_products()


def _format_product_messages(products: list[dict[str, Any]]) -> list[str]:
    lines = ["Товары WB (цены показаны как значения из API):"]
    for product in products:
        card = product.get("card") or {}
        pricing = product.get("pricing") or {}
        nm_id = card.get("nmID", "?")
        title = str(card.get("title") or card.get("vendorCode") or nm_id).replace("\n", " ")[:100]
        line = f"• {title} — артикул {nm_id}"
        sizes = pricing.get("sizes") if isinstance(pricing, dict) else None
        details = []
        if isinstance(sizes, list):
            for size in sizes[:3]:
                if not isinstance(size, dict):
                    continue
                values = []
                for key, label in (("price", "цена"), ("discountedPrice", "после скидки"), ("discount", "скидка %")):
                    if key in size:
                        values.append(f"{label} {size[key]}")
                if values:
                    details.append(", ".join(values))
        if details:
            line += "\n  " + "; ".join(details)
        else:
            line += "\n  Цена недоступна"
        lines.append(line)

    messages: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > MAX_TELEGRAM_MESSAGE and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
