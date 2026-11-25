"""Interactive seller management use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from margin_guard.application.dto import SellerStatusDTO
from margin_guard.application.ports import (
    MarketplaceGatewayPort,
    ProductRepositoryPort,
    SecretCipherPort,
    UserRepositoryPort,
)
from margin_guard.domain.entities import CatalogProduct, SellerAccount
from margin_guard.domain.exceptions import DomainValidationError, EntityNotFoundError
from margin_guard.domain.value_objects import (
    MAX_MONEY_AMOUNT,
    MAX_THRESHOLD_PERCENT,
    MIN_THRESHOLD_PERCENT,
    ZERO,
    parse_bounded_decimal,
    parse_fee_assignments,
    validate_marketplace_token,
    validate_product_id,
)


class RegisterSellerUseCase:
    """Ensure a seller profile exists on ``/start``."""

    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, telegram_id: int) -> SellerAccount:
        return await asyncio.to_thread(self._users.get_or_create, telegram_id)


class ConnectMarketplaceTokenUseCase:
    """Validate, context-encrypt, and persist a read-only WB API token."""

    def __init__(self, users: UserRepositoryPort, cipher: SecretCipherPort) -> None:
        self._users = users
        self._cipher = cipher

    async def execute(self, telegram_id: int, raw_token: str) -> None:
        clean_token = validate_marketplace_token(raw_token)
        encrypted = self._cipher.encrypt(clean_token, subject=str(telegram_id))
        await asyncio.to_thread(self._users.save_encrypted_token, telegram_id, encrypted)


class SyncSellerProductsUseCase:
    """Decrypt seller token, fetch catalog from WB, and update cached products."""

    def __init__(
        self,
        users: UserRepositoryPort,
        products: ProductRepositoryPort,
        cipher: SecretCipherPort,
        gateway: MarketplaceGatewayPort,
    ) -> None:
        self._users = users
        self._products = products
        self._cipher = cipher
        self._gateway = gateway

    async def execute(self, telegram_id: int) -> Sequence[CatalogProduct]:
        seller = await asyncio.to_thread(self._users.get_by_telegram_id, telegram_id)
        if seller is None or not seller.encrypted_wb_token:
            raise EntityNotFoundError("Сначала подключите WB API токен командой /connect.")

        token = self._cipher.decrypt(
            seller.encrypted_wb_token, subject=str(telegram_id)
        )
        fetched = await asyncio.to_thread(self._gateway.fetch_catalog_products, token)
        await asyncio.to_thread(
            self._products.upsert_catalog_products, seller.id, fetched
        )
        return fetched


class SetProductCostUseCase:
    """Validate and save unit cost price for one cached product."""

    def __init__(
        self, users: UserRepositoryPort, products: ProductRepositoryPort
    ) -> None:
        self._users = users
        self._products = products

    async def execute(self, telegram_id: int, raw_nm_id: str, raw_cost: str) -> Decimal:
        nm_id = validate_product_id(raw_nm_id)
        cost = parse_bounded_decimal(
            raw_cost,
            field_label="себестоимость",
            min_value=ZERO,
            max_value=MAX_MONEY_AMOUNT,
        )
        seller = await asyncio.to_thread(self._users.get_by_telegram_id, telegram_id)
        if seller is None:
            raise EntityNotFoundError(
                "Сначала выполните /start, подключите WB токен и загрузите товары через /products."
            )
        updated = await asyncio.to_thread(
            self._products.set_cost_price, seller.id, nm_id, cost
        )
        if not updated:
            raise EntityNotFoundError(
                "Товар не найден. Сначала загрузите список через /products и проверьте артикул WB."
            )
        return cost


class SetMarginThresholdUseCase:
    """Validate and save the seller's alert margin percentage threshold."""

    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, telegram_id: int, raw_threshold: str) -> Decimal:
        threshold = parse_bounded_decimal(
            raw_threshold,
            field_label="порог маржи",
            min_value=MIN_THRESHOLD_PERCENT,
            max_value=MAX_THRESHOLD_PERCENT,
        )
        await asyncio.to_thread(self._users.set_threshold, telegram_id, threshold)
        return threshold


class ConfigureExpensesUseCase:
    """Parse and persist default or per-product seller fee rules."""

    def __init__(
        self, users: UserRepositoryPort, products: ProductRepositoryPort
    ) -> None:
        self._users = users
        self._products = products

    async def execute(
        self, telegram_id: int, scope: str, assignments: Sequence[str]
    ) -> str:
        clean_scope = scope.strip()
        if clean_scope == "default":
            parsed = parse_fee_assignments(assignments, allow_reset_to_default=False)
            await asyncio.to_thread(
                self._users.update_default_expenses, telegram_id, parsed
            )
            return "default"

        if not clean_scope.isdigit():
            raise DomainValidationError(
                "Первым параметром укажите default или числовой артикул WB."
            )
        nm_id = validate_product_id(clean_scope)
        parsed = parse_fee_assignments(assignments, allow_reset_to_default=True)
        seller = await asyncio.to_thread(self._users.get_by_telegram_id, telegram_id)
        if seller is None:
            raise EntityNotFoundError("Товар не найден. Сначала загрузите его командой /products.")
        updated = await asyncio.to_thread(
            self._products.update_expense_overrides, seller.id, nm_id, parsed
        )
        if not updated:
            raise EntityNotFoundError("Товар не найден. Сначала загрузите его командой /products.")
        return nm_id


class GetSellerStatusUseCase:
    """Assemble seller monitoring status summary."""

    def __init__(
        self, users: UserRepositoryPort, products: ProductRepositoryPort
    ) -> None:
        self._users = users
        self._products = products

    async def execute(self, telegram_id: int) -> SellerStatusDTO:
        seller = await asyncio.to_thread(self._users.get_by_telegram_id, telegram_id)
        if seller is None:
            raise EntityNotFoundError("Профиль ещё не создан. Выполните /start.")
        items = await asyncio.to_thread(self._products.list_by_user, seller.id)
        return SellerStatusDTO(
            has_token=seller.has_token,
            products_count=len(items),
            margin_threshold=seller.margin_threshold,
            monitoring_enabled=seller.monitoring_enabled,
            missing_required_expenses=seller.missing_required_defaults,
        )


class PauseMonitoringUseCase:
    """Pause periodic checks for a seller."""

    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, telegram_id: int) -> bool:
        return await asyncio.to_thread(
            self._users.set_monitoring_enabled, telegram_id, False
        )


class ResumeMonitoringUseCase:
    """Resume periodic checks once a seller has connected a token."""

    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, telegram_id: int) -> None:
        seller = await asyncio.to_thread(self._users.get_by_telegram_id, telegram_id)
        if seller is None:
            raise EntityNotFoundError("Сначала выполните /start.")
        if not seller.has_token:
            raise DomainValidationError(
                "Сначала подключите WB API токен командой /connect."
            )
        await asyncio.to_thread(self._users.set_monitoring_enabled, telegram_id, True)


class DeleteSellerAccountUseCase:
    """Permanently delete a seller's encrypted token and cached catalog data."""

    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, telegram_id: int) -> bool:
        return await asyncio.to_thread(self._users.delete_by_telegram_id, telegram_id)


@dataclass(frozen=True, slots=True)
class SellerUseCases:
    """Grouped interactive use cases injected into delivery adapters."""

    register: RegisterSellerUseCase
    connect_token: ConnectMarketplaceTokenUseCase
    sync_products: SyncSellerProductsUseCase
    set_cost: SetProductCostUseCase
    set_threshold: SetMarginThresholdUseCase
    configure_expenses: ConfigureExpensesUseCase
    get_status: GetSellerStatusUseCase
    pause_monitoring: PauseMonitoringUseCase
    resume_monitoring: ResumeMonitoringUseCase
    delete_account: DeleteSellerAccountUseCase

    @classmethod
    def build(
        cls,
        *,
        users: UserRepositoryPort,
        products: ProductRepositoryPort,
        cipher: SecretCipherPort,
        gateway: MarketplaceGatewayPort,
    ) -> SellerUseCases:
        return cls(
            register=RegisterSellerUseCase(users),
            connect_token=ConnectMarketplaceTokenUseCase(users, cipher),
            sync_products=SyncSellerProductsUseCase(users, products, cipher, gateway),
            set_cost=SetProductCostUseCase(users, products),
            set_threshold=SetMarginThresholdUseCase(users),
            configure_expenses=ConfigureExpensesUseCase(users, products),
            get_status=GetSellerStatusUseCase(users, products),
            pause_monitoring=PauseMonitoringUseCase(users),
            resume_monitoring=ResumeMonitoringUseCase(users),
            delete_account=DeleteSellerAccountUseCase(users),
        )
