"""Application-layer Data Transfer Objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from margin_guard.domain.value_objects import ExpenseCategory


@dataclass(frozen=True, slots=True)
class SellerStatusDTO:
    """Read model returned to presentation handlers for ``/status``."""

    has_token: bool
    products_count: int
    margin_threshold: Decimal
    monitoring_enabled: bool
    missing_required_expenses: tuple[ExpenseCategory, ...]


@dataclass(frozen=True, slots=True)
class MonitoringCycleReport:
    """Operational summary of a single scheduled monitoring run."""

    checked_sellers: int = 0
    alerts_delivered: int = 0
    paused_sellers: int = 0
    isolated_failures: int = 0
