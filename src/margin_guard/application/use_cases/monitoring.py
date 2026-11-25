"""Scheduled background monitoring use case with strict per-seller fault isolation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from margin_guard.application.dto import MonitoringCycleReport
from margin_guard.application.ports import (
    AlertNotifierPort,
    MarketplaceGatewayPort,
    ProductRepositoryPort,
    SecretCipherPort,
    UserRepositoryPort,
)
from margin_guard.domain.entities import MarginAlertItem, SellerAccount
from margin_guard.domain.exceptions import (
    MarketplaceAuthError,
    MarketplaceError,
    MarketplacePermissionError,
    SecretProtectionError,
)
from margin_guard.domain.services import evaluate_margin_state, evaluate_product_margin
from margin_guard.domain.value_objects import MarginState

logger = logging.getLogger("margin_guard.monitoring")


@dataclass(frozen=True, slots=True)
class _PendingStateUpdate:
    nm_id: str
    state: MarginState
    margin: Decimal
    price: Decimal
    notify: bool


class RunMonitoringCycleUseCase:
    """Evaluate active sellers, send grouped low-margin alerts, and isolate failures."""

    def __init__(
        self,
        *,
        users: UserRepositoryPort,
        products: ProductRepositoryPort,
        cipher: SecretCipherPort,
        gateway: MarketplaceGatewayPort,
        notifier: AlertNotifierPort,
        per_seller_timeout_seconds: float = 60.0,
        max_concurrent_sellers: int = 4,
    ) -> None:
        self._users = users
        self._products = products
        self._cipher = cipher
        self._gateway = gateway
        self._notifier = notifier
        self._per_seller_timeout = max(1.0, per_seller_timeout_seconds)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent_sellers))

    async def execute(self) -> MonitoringCycleReport:
        sellers = await asyncio.to_thread(self._users.list_active_monitoring_sellers)
        if not sellers:
            return MonitoringCycleReport()

        outcomes = await asyncio.gather(
            *(self._check_seller_isolated(seller) for seller in sellers)
        )
        checked = len(sellers)
        alerts_delivered = sum(1 for item in outcomes if item == "alerted")
        paused = sum(1 for item in outcomes if item == "paused")
        failures = sum(1 for item in outcomes if item == "failed")
        return MonitoringCycleReport(
            checked_sellers=checked,
            alerts_delivered=alerts_delivered,
            paused_sellers=paused,
            isolated_failures=failures,
        )

    async def _check_seller_isolated(self, seller: SellerAccount) -> str:
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._process_seller(seller),
                    timeout=self._per_seller_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "Monitoring check timed out for seller_id=%s", seller.id
                )
                return "failed"
            except Exception as exc:
                logger.error(
                    "Isolated unexpected error for seller_id=%s (%s)",
                    seller.id,
                    type(exc).__name__,
                )
                return "failed"

    async def _process_seller(self, seller: SellerAccount) -> str:
        if not seller.encrypted_wb_token:
            return "ok"

        try:
            token = self._cipher.decrypt(
                seller.encrypted_wb_token, subject=str(seller.telegram_id)
            )
        except SecretProtectionError:
            await asyncio.to_thread(
                self._users.set_monitoring_enabled, seller.telegram_id, False
            )
            await self._notifier.send_system_notice(
                seller.telegram_id,
                "Не удалось расшифровать WB токен. Проверки приостановлены; "
                "проверьте ключ шифрования и подключите токен снова командой /connect.",
            )
            return "paused"

        try:
            catalog = await asyncio.to_thread(
                self._gateway.fetch_catalog_products, token
            )
        except MarketplaceAuthError:
            await asyncio.to_thread(
                self._users.set_monitoring_enabled, seller.telegram_id, False
            )
            await self._notifier.send_system_notice(
                seller.telegram_id,
                "Wildberries отклонил токен. Проверки приостановлены. "
                "Подключите действующий токен только на чтение командой /connect.",
            )
            return "paused"
        except MarketplacePermissionError:
            await asyncio.to_thread(
                self._users.set_monitoring_enabled, seller.telegram_id, False
            )
            await self._notifier.send_system_notice(
                seller.telegram_id,
                "У WB токена нет нужных прав. Проверки приостановлены. "
                "Проверьте доступы Контент и Цены и скидки, затем подключите токен через /connect.",
            )
            return "paused"
        except MarketplaceError as exc:
            logger.info(
                "Transient marketplace error for seller_id=%s (%s)",
                seller.id,
                type(exc).__name__,
            )
            return "ok"

        await asyncio.to_thread(
            self._products.upsert_catalog_products, seller.id, catalog
        )
        stored_items = await asyncio.to_thread(self._products.list_by_user, seller.id)
        stored_by_id = {item.nm_id: item for item in stored_items}

        alerts: list[MarginAlertItem] = []
        updates: list[_PendingStateUpdate] = []

        for catalog_item in catalog:
            stored = stored_by_id.get(catalog_item.nm_id)
            if stored is None:
                continue
            observation = evaluate_product_margin(
                catalog_item, stored, seller.expense_defaults
            )
            if observation is None:
                continue
            margin, sale_price = observation
            decision = evaluate_margin_state(
                margin, seller.margin_threshold, stored.last_margin_state
            )
            if decision.state is None:
                continue
            if decision.notify and stored.last_margin is not None:
                alerts.append(
                    MarginAlertItem(
                        nm_id=catalog_item.nm_id,
                        title=catalog_item.display_title,
                        sale_price=sale_price,
                        previous_margin=stored.last_margin,
                        current_margin=margin,
                    )
                )
            updates.append(
                _PendingStateUpdate(
                    nm_id=catalog_item.nm_id,
                    state=decision.state,
                    margin=margin,
                    price=sale_price,
                    notify=decision.notify,
                )
            )

        delivered = True
        if alerts:
            delivered = await self._notifier.send_margin_alert(
                seller.telegram_id, alerts, seller.margin_threshold
            )

        for pending in updates:
            if pending.notify and not delivered:
                continue
            await asyncio.to_thread(
                self._products.save_monitoring_state,
                seller.id,
                pending.nm_id,
                pending.state,
                pending.margin,
                pending.price,
            )

        return "alerted" if (alerts and delivered) else "ok"
