"""Pure domain services for margin math and alert state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from .entities import CatalogProduct, StoredProduct
from .value_objects import (
    HUNDRED,
    TWO_PLACES,
    ZERO,
    ExpenseCategory,
    ExpenseRule,
    ExpenseSheet,
    MarginDecision,
    MarginState,
)


def calculate_margin(
    revenue: Decimal,
    cost_price: Decimal | None,
    *,
    expenses: ExpenseSheet | None = None,
    commission: Decimal = ZERO,
    logistics: Decimal = ZERO,
    tax: Decimal = ZERO,
    storage: Decimal = ZERO,
    advertising: Decimal = ZERO,
    commission_percent: Decimal = ZERO,
    logistics_percent: Decimal = ZERO,
    tax_percent: Decimal = ZERO,
    storage_percent: Decimal = ZERO,
    advertising_percent: Decimal = ZERO,
) -> Decimal | None:
    """Return margin percent rounded half-up to 0.01, or None when cost is unknown.

    Accepts either a cohesive ``ExpenseSheet`` value object or individual Decimal
    keyword parameters. Floats are strictly rejected to guarantee exact monetary math.
    """
    if cost_price is None:
        return None

    checked_decimals: dict[str, Decimal] = {
        "revenue": revenue,
        "cost_price": cost_price,
        "commission": commission,
        "logistics": logistics,
        "tax": tax,
        "storage": storage,
        "advertising": advertising,
        "commission_percent": commission_percent,
        "logistics_percent": logistics_percent,
        "tax_percent": tax_percent,
        "storage_percent": storage_percent,
        "advertising_percent": advertising_percent,
    }
    for name, value in checked_decimals.items():
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal")
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        if name != "revenue" and value < ZERO:
            raise ValueError(f"{name} cannot be negative")

    if revenue <= ZERO:
        raise ValueError("revenue must be greater than zero")

    sheet = expenses or ExpenseSheet(
        commission=ExpenseRule(fixed=commission, percent=commission_percent),
        logistics=ExpenseRule(fixed=logistics, percent=logistics_percent),
        tax=ExpenseRule(fixed=tax, percent=tax_percent),
        storage=ExpenseRule(fixed=storage, percent=storage_percent),
        advertising=ExpenseRule(fixed=advertising, percent=advertising_percent),
    )
    total_expenses = cost_price + sheet.total_for_revenue(revenue)
    profit = revenue - total_expenses
    return (profit / revenue * HUNDRED).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def evaluate_margin_state(
    margin: Decimal | None,
    threshold: Decimal,
    previous_state: MarginState | str | None,
) -> MarginDecision:
    """Evaluate state transitions and fire alerts strictly on ``ok -> below_threshold``."""
    if margin is not None and (not isinstance(margin, Decimal) or not margin.is_finite()):
        raise TypeError("margin must be a finite Decimal or None")
    if not isinstance(threshold, Decimal) or not threshold.is_finite():
        raise TypeError("threshold must be a finite Decimal")

    normalized_prev: MarginState | None
    if previous_state is None:
        normalized_prev = None
    elif isinstance(previous_state, MarginState):
        normalized_prev = previous_state
    elif previous_state in (MarginState.OK.value, MarginState.BELOW_THRESHOLD.value):
        normalized_prev = MarginState(previous_state)
    else:
        normalized_prev = None

    if margin is None:
        return MarginDecision(state=normalized_prev, changed=False, notify=False)

    current_state = (
        MarginState.BELOW_THRESHOLD if margin < threshold else MarginState.OK
    )
    return MarginDecision(
        state=current_state,
        changed=current_state != normalized_prev,
        notify=(
            normalized_prev == MarginState.OK
            and current_state == MarginState.BELOW_THRESHOLD
        ),
    )


def evaluate_product_margin(
    catalog_product: CatalogProduct,
    stored_product: StoredProduct,
    default_expenses: Mapping[ExpenseCategory, ExpenseRule],
) -> tuple[Decimal, Decimal] | None:
    """Compute the lowest size-level (margin, discounted_price) pair for a product."""
    if stored_product.cost_price is None or stored_product.cost_price < ZERO:
        return None

    sheet = ExpenseSheet.resolve(default_expenses, stored_product.expense_overrides)
    if sheet is None:
        return None

    observations: list[tuple[Decimal, Decimal]] = []
    for size in catalog_product.sizes:
        revenue = size.discounted_price
        if revenue is None or not revenue.is_finite() or revenue <= ZERO:
            continue
        margin = calculate_margin(revenue, stored_product.cost_price, expenses=sheet)
        if margin is not None:
            observations.append((margin, revenue))

    if not observations:
        return None
    return min(observations, key=lambda item: item[0])
