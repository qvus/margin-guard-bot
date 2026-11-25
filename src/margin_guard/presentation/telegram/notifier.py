"""Telegram implementation of AlertNotifierPort."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any

from margin_guard.domain.entities import MarginAlertItem
from margin_guard.presentation.telegram.formatters import format_margin_alert_summary

logger = logging.getLogger("margin_guard.telegram.notifier")

TelegramSendFunc = Callable[[int, str], Awaitable[Any]]


class TelegramAlertNotifier:
    """Deliver alerts via Telegram while containing delivery exceptions safely."""

    def __init__(self, send_message: TelegramSendFunc) -> None:
        self._send_message = send_message

    async def send_margin_alert(
        self,
        telegram_id: int,
        alerts: Sequence[MarginAlertItem],
        threshold: Decimal,
    ) -> bool:
        summary = format_margin_alert_summary(alerts, threshold)
        return await self.send_system_notice(telegram_id, summary)

    async def send_system_notice(self, telegram_id: int, text: str) -> bool:
        try:
            await self._send_message(telegram_id, text)
        except Exception as exc:
            logger.warning(
                "Telegram delivery failed (%s)",
                type(exc).__name__,
            )
            return False
        return True
