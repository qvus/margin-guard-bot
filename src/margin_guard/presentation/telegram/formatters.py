"""Pure message formatters for Telegram responses and alert summaries."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from margin_guard.application.dto import SellerStatusDTO
from margin_guard.domain.entities import CatalogProduct, MarginAlertItem
from margin_guard.domain.value_objects import format_decimal, sanitize_display_text

MAX_TELEGRAM_MESSAGE_LENGTH = 3500


def format_product_catalog_messages(
    products: Sequence[CatalogProduct],
    *,
    max_length: int = MAX_TELEGRAM_MESSAGE_LENGTH,
) -> list[str]:
    """Render seller products and size prices into bounded Telegram message chunks."""
    lines = ["Товары WB (цены показаны как значения из API):"]
    for product in products:
        line = f"• {product.display_title} — артикул {product.nm_id}"
        details: list[str] = []
        for size in product.sizes[:3]:
            parts: list[str] = []
            if size.price is not None:
                parts.append(f"цена {format_decimal(size.price)}")
            if size.discounted_price is not None:
                parts.append(f"после скидки {format_decimal(size.discounted_price)}")
            if size.discount_percent is not None:
                parts.append(f"скидка % {size.discount_percent}")
            if parts:
                details.append(", ".join(parts))
        if details:
            line += "\n  " + "; ".join(details)
        else:
            line += "\n  Цена недоступна"
        lines.append(line)

    messages: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > max_length and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def format_margin_alert_summary(
    alerts: Sequence[MarginAlertItem],
    threshold: Decimal,
    *,
    max_length: int = MAX_TELEGRAM_MESSAGE_LENGTH,
) -> str:
    """Format grouped low-margin transitions within Telegram's safe message limit."""
    threshold_str = format_decimal(threshold)
    lines = [f"⚠️ Маржа ниже порога {threshold_str}%:"]
    for item in alerts:
        safe_title = sanitize_display_text(item.title, fallback=item.nm_id, max_length=100)
        price_str = format_decimal(item.sale_price)
        prev_str = f"{item.previous_margin:.2f}"
        curr_str = f"{item.current_margin:.2f}"
        lines.append(
            f"• {safe_title} (артикул {item.nm_id}): цена {price_str}; "
            f"маржа {prev_str}% → {curr_str}% "
            f"(порог {threshold_str}%)."
        )

    full_message = "\n".join(lines)
    if len(full_message) <= max_length:
        return full_message

    included = lines[:1]
    for index, line in enumerate(lines[1:], start=1):
        notice = f"\n… ещё {len(lines) - index} товар(ов); список сокращён."
        candidate = "\n".join([*included, line]) + notice
        if len(candidate) > max_length:
            break
        included.append(line)
    omitted = len(lines) - len(included)
    included.append(f"… ещё {omitted} товар(ов); список сокращён.")
    return "\n".join(included)


def format_seller_status(status: SellerStatusDTO) -> str:
    """Format the seller's current configuration and monitoring status."""
    connected = "подключён" if status.has_token else "не подключён"
    monitoring = "включён" if status.monitoring_enabled else "приостановлен"
    if status.missing_required_expenses:
        missing_names = ", ".join(cat.value for cat in status.missing_required_expenses)
        expense_status = f"Не заданы общие {missing_names}; настройте /fees"
    else:
        expense_status = "Общие расходы настроены"

    return (
        f"WB токен: {connected}\n"
        f"Товаров в списке: {status.products_count}\n"
        f"Порог маржи: {format_decimal(status.margin_threshold)}%\n"
        f"Мониторинг: {monitoring}\n"
        f"Расходы: {expense_status}"
    )
