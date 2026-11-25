"""Periodic margin checks and one-message-per-user alert aggregation."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import repository
from .monitoring import calculate_product_margin, evaluate_margin_state
from .token_encryption import TokenCipher, TokenEncryptionError
from .wildberries import (
    WBApiError,
    WBAuthenticationError,
    WBPermissionError,
    WildberriesClient,
)

AlertSender = Callable[[int, str], Awaitable[None]]
ProductFetcher = Callable[[str], list[dict[str, Any]]]
MAX_ALERT_LENGTH = 3500


def _fetch_products(token: str) -> list[dict[str, Any]]:
    with WildberriesClient(token) as client:
        return client.list_products()


async def run_monitoring_cycle(
    database_path: str | Path,
    cipher: TokenCipher,
    send_alert: AlertSender,
    *,
    fetch_products: ProductFetcher | None = None,
) -> None:
    """Check each active seller and send one grouped alert on state changes."""
    fetcher = fetch_products or _fetch_products
    users = repository.list_monitoring_users(database_path)
    for user in users:
        telegram_id = user["telegram_id"]
        try:
            token = cipher.decrypt(user["wb_token_encrypted"])
        except TokenEncryptionError:
            repository.set_monitoring_enabled(database_path, telegram_id, False)
            await _send(
                send_alert,
                telegram_id,
                "Не удалось расшифровать WB токен. Проверки приостановлены; "
                "проверьте ключ шифрования и подключите токен снова командой /connect.",
            )
            continue

        try:
            products = await asyncio.to_thread(fetcher, token)
        except WBAuthenticationError:
            repository.set_monitoring_enabled(database_path, telegram_id, False)
            await _send(
                send_alert,
                telegram_id,
                "Wildberries отклонил токен. Проверки приостановлены. "
                "Подключите действующий токен только на чтение командой /connect.",
            )
            continue
        except WBPermissionError:
            repository.set_monitoring_enabled(database_path, telegram_id, False)
            await _send(
                send_alert,
                telegram_id,
                "У WB токена нет нужных прав. Проверки приостановлены. "
                "Проверьте доступы Контент и Цены и скидки, затем подключите токен через /connect.",
            )
            continue
        except WBApiError:
            # Temporary API errors do not change state; the next scheduled cycle retries.
            continue

        repository.save_products(database_path, user["id"], products)
        cached = repository.list_products(database_path, user["id"])
        cached_by_id = {row["marketplace_product_id"]: row for row in cached}
        threshold = Decimal(user["margin_threshold"])
        alerts: list[tuple[str, str, Decimal, Decimal, Decimal]] = []
        results: list[tuple[str, str, Decimal, Decimal, bool]] = []

        for product in products:
            card = product.get("card")
            if not isinstance(card, dict) or card.get("nmID") is None:
                continue
            nm_id = str(card["nmID"])
            row = cached_by_id.get(nm_id)
            if row is None:
                continue
            observation = calculate_product_margin(
                product, row, user["expense_defaults_json"]
            )
            if observation is None:
                continue
            margin, price = observation
            decision = evaluate_margin_state(
                margin, threshold, row["last_margin_state"]
            )
            if decision.state is None:
                continue
            if decision.notify and row["last_margin"] is not None:
                title = str(card.get("title") or card.get("vendorCode") or nm_id)
                old_margin = Decimal(row["last_margin"])
                alerts.append((nm_id, title, price, old_margin, margin))
            results.append(
                (
                    nm_id,
                    decision.state,
                    margin,
                    price,
                    decision.notify,
                )
            )

        delivered = True
        if alerts:
            delivered = await _send(
                send_alert,
                telegram_id,
                _format_alert_summary(alerts, threshold),
            )
        for nm_id, state, margin, price, notify in results:
            # Keep an alerting product's previous state if Telegram delivery
            # failed, so the next cycle can retry that state transition.
            if notify and not delivered:
                continue
            repository.save_product_monitoring_state(
                database_path,
                user["id"],
                nm_id,
                state,
                format(margin, "f"),
                format(price, "f"),
            )


async def _send(sender: AlertSender, telegram_id: int, text: str) -> bool:
    try:
        await sender(telegram_id, text)
    except Exception:
        # A failed Telegram delivery must not stop checks for other sellers.
        return False
    return True


def _format_alert_summary(
    alerts: list[tuple[str, str, Decimal, Decimal, Decimal]], threshold: Decimal
) -> str:
    lines = [f"⚠️ Маржа ниже порога {format(threshold, 'f')}%:"]
    for nm_id, title, price, before, after in alerts:
        safe_title = title.replace("\n", " ")[:100]
        lines.append(
            f"• {safe_title} (артикул {nm_id}): цена {format(price, 'f')}; "
            f"маржа {format(before, 'f')}% → {format(after, 'f')}% "
            f"(порог {format(threshold, 'f')}%)."
        )
    message = "\n".join(lines)
    if len(message) <= MAX_ALERT_LENGTH:
        return message

    included = lines[:1]
    for index, line in enumerate(lines[1:], start=1):
        notice = f"\n… ещё {len(lines) - index} товар(ов); список сокращён."
        candidate = "\n".join(included + [line]) + notice
        if len(candidate) > MAX_ALERT_LENGTH:
            break
        included.append(line)
    omitted = len(lines) - len(included)
    included.append(f"… ещё {omitted} товар(ов); список сокращён.")
    return "\n".join(included)
