"""Inbound/outbound ports (Protocols) inverting infrastructure dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol

from margin_guard.domain.entities import (
    CatalogProduct,
    MarginAlertItem,
    SellerAccount,
    StoredProduct,
)
from margin_guard.domain.value_objects import ExpenseCategory, ExpenseRule, MarginState


class SecretCipherPort(Protocol):
    """Authenticated encryption port binding secrets to their owner context."""

    def encrypt(self, plaintext: str, *, subject: str) -> str:
        """Encrypt plaintext bound to ``subject`` (e.g. seller Telegram ID)."""

    def decrypt(self, ciphertext: str, *, subject: str) -> str:
        """Decrypt ciphertext and verify that it belongs to ``subject``."""


class UserRepositoryPort(Protocol):
    """Persistence port for seller accounts and monitoring settings."""

    def get_or_create(self, telegram_id: int) -> SellerAccount: ...

    def get_by_telegram_id(self, telegram_id: int) -> SellerAccount | None: ...

    def save_encrypted_token(self, telegram_id: int, encrypted_token: str) -> None: ...

    def set_threshold(self, telegram_id: int, threshold: Decimal) -> None: ...

    def update_default_expenses(
        self,
        telegram_id: int,
        updates: Mapping[ExpenseCategory, ExpenseRule | None],
    ) -> None: ...

    def set_monitoring_enabled(self, telegram_id: int, enabled: bool) -> bool: ...

    def list_active_monitoring_sellers(self) -> Sequence[SellerAccount]: ...

    def delete_by_telegram_id(self, telegram_id: int) -> bool: ...


class ProductRepositoryPort(Protocol):
    """Persistence port for cached catalog products and alert state."""

    def upsert_catalog_products(
        self, user_id: int, products: Sequence[CatalogProduct]
    ) -> int: ...

    def list_by_user(self, user_id: int) -> Sequence[StoredProduct]: ...

    def set_cost_price(self, user_id: int, nm_id: str, cost_price: Decimal) -> bool: ...

    def update_expense_overrides(
        self,
        user_id: int,
        nm_id: str,
        updates: Mapping[ExpenseCategory, ExpenseRule | None],
    ) -> bool: ...

    def save_monitoring_state(
        self,
        user_id: int,
        nm_id: str,
        state: MarginState,
        margin: Decimal,
        price: Decimal,
    ) -> None: ...


class MarketplaceGatewayPort(Protocol):
    """Read-only gateway to fetch seller catalog cards and prices."""

    def fetch_catalog_products(self, token: str) -> Sequence[CatalogProduct]: ...


class AlertNotifierPort(Protocol):
    """Outbound delivery port for grouped margin alerts and system notices."""

    async def send_margin_alert(
        self,
        telegram_id: int,
        alerts: Sequence[MarginAlertItem],
        threshold: Decimal,
    ) -> bool: ...

    async def send_system_notice(self, telegram_id: int, text: str) -> bool: ...


class RateLimiterPort(Protocol):
    """Per-actor sliding-window rate limiter port."""

    def check(
        self, key: str, *, max_requests: int, window_seconds: float
    ) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``."""
