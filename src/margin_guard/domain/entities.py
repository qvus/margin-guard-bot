"""Core domain entities representing sellers, products, and alerts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from .value_objects import (
    REQUIRED_EXPENSE_CATEGORIES,
    ExpenseCategory,
    ExpenseRule,
    MarginState,
    sanitize_display_text,
)


@dataclass(frozen=True, slots=True)
class ProductSizePrice:
    """Selling price snapshot for a single product size."""

    price: Decimal | None = None
    discounted_price: Decimal | None = None
    discount_percent: int | None = None
    chrt_id: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    """Read-only product card and pricing snapshot fetched from Wildberries."""

    nm_id: str
    vendor_code: str | None = None
    title: str | None = None
    sizes: tuple[ProductSizePrice, ...] = ()

    @property
    def display_title(self) -> str:
        """Return a control-character-free human-readable product title."""
        candidate = self.title or self.vendor_code or self.nm_id
        return sanitize_display_text(candidate, fallback=self.nm_id, max_length=100)


@dataclass(frozen=True, slots=True)
class StoredProduct:
    """Seller product persisted with cost, fee overrides, and last alert state."""

    nm_id: str
    vendor_code: str | None = None
    title: str | None = None
    sizes: tuple[ProductSizePrice, ...] = ()
    cost_price: Decimal | None = None
    expense_overrides: Mapping[ExpenseCategory, ExpenseRule] = field(default_factory=dict)
    last_margin_state: MarginState | None = None
    last_margin: Decimal | None = None
    last_price: Decimal | None = None

    @property
    def display_title(self) -> str:
        """Return a control-character-free human-readable product title."""
        candidate = self.title or self.vendor_code or self.nm_id
        return sanitize_display_text(candidate, fallback=self.nm_id, max_length=100)


@dataclass(frozen=True, slots=True)
class SellerAccount:
    """Seller profile and monitoring configuration."""

    id: int
    telegram_id: int
    encrypted_wb_token: str | None
    margin_threshold: Decimal
    expense_defaults: Mapping[ExpenseCategory, ExpenseRule]
    monitoring_enabled: bool

    @property
    def has_token(self) -> bool:
        return bool(self.encrypted_wb_token)

    @property
    def missing_required_defaults(self) -> tuple[ExpenseCategory, ...]:
        return tuple(
            category
            for category in REQUIRED_EXPENSE_CATEGORIES
            if category not in self.expense_defaults
        )


@dataclass(frozen=True, slots=True)
class MarginAlertItem:
    """Single product entry within a grouped low-margin alert notification."""

    nm_id: str
    title: str
    sale_price: Decimal
    previous_margin: Decimal
    current_margin: Decimal
