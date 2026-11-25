"""Pure state transition rules for margin alerts."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Literal

from .margin import calculate_margin

MarginState = Literal["ok", "below_threshold"]
EXPENSE_NAMES = ("commission", "logistics", "tax", "storage", "advertising")
REQUIRED_EXPENSES = ("commission", "logistics", "tax")
ZERO = Decimal("0")


@dataclass(frozen=True)
class MarginDecision:
    """State to persist and whether this transition should send an alert."""

    state: MarginState | None
    changed: bool
    notify: bool


def evaluate_margin_state(
    margin: Decimal | None,
    threshold: Decimal,
    previous_state: MarginState | None,
) -> MarginDecision:
    """Detect the transition from at/above threshold to below threshold.

    Unknown margins leave the saved state untouched. The initial measurement
    establishes state without sending an alert; alerts fire only on an observed
    ``ok`` -> ``below_threshold`` transition. Equality with the threshold is OK.
    """
    if margin is not None and not isinstance(margin, Decimal):
        raise TypeError("margin must be Decimal or None")
    if not isinstance(threshold, Decimal):
        raise TypeError("threshold must be Decimal")
    if margin is None:
        return MarginDecision(state=previous_state, changed=False, notify=False)

    state: MarginState = "below_threshold" if margin < threshold else "ok"
    return MarginDecision(
        state=state,
        changed=state != previous_state,
        notify=previous_state == "ok" and state == "below_threshold",
    )


def calculate_product_margin(
    product: dict,
    product_row: object,
    default_expenses_json: str,
) -> tuple[Decimal, Decimal] | None:
    """Return the lowest size-level margin and its sale price, or None.

    The seller discount price is used for each size. Commission, logistics and
    tax must be provided per product or as user defaults. Storage and ads are
    optional and are treated as zero only when the seller has left them unset.
    """
    cost_text = product_row["cost_price"]  # type: ignore[index]
    if cost_text is None:
        return None
    try:
        cost = Decimal(str(cost_text))
        defaults = json.loads(default_expenses_json or "{}")
        overrides = json.loads(product_row["expense_overrides_json"] or "{}")  # type: ignore[index]
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not cost.is_finite() or cost < ZERO or not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return None

    expense_values: dict[str, tuple[Decimal, Decimal]] = {}
    for name in EXPENSE_NAMES:
        item = overrides.get(name, defaults.get(name))
        if item is None:
            if name in REQUIRED_EXPENSES:
                return None
            expense_values[name] = (ZERO, ZERO)
            continue
        if not isinstance(item, dict):
            return None
        try:
            fixed = Decimal(str(item.get("fixed", "0")))
            percent = Decimal(str(item.get("percent", "0")))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not fixed.is_finite() or not percent.is_finite() or fixed < ZERO or percent < ZERO:
            return None
        expense_values[name] = (fixed, percent)

    pricing = product.get("pricing")
    sizes = pricing.get("sizes") if isinstance(pricing, dict) else None
    if not isinstance(sizes, list):
        return None

    observations: list[tuple[Decimal, Decimal]] = []
    for size in sizes:
        if not isinstance(size, dict) or "discountedPrice" not in size:
            continue
        try:
            revenue = Decimal(str(size["discountedPrice"]))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not revenue.is_finite() or revenue <= ZERO:
            continue
        values = {
            name: expense_values[name]
            for name in EXPENSE_NAMES
        }
        margin = calculate_margin(
            revenue,
            cost,
            commission=values["commission"][0],
            commission_percent=values["commission"][1],
            logistics=values["logistics"][0],
            logistics_percent=values["logistics"][1],
            tax=values["tax"][0],
            tax_percent=values["tax"][1],
            storage=values["storage"][0],
            storage_percent=values["storage"][1],
            advertising=values["advertising"][0],
            advertising_percent=values["advertising"][1],
        )
        if margin is not None:
            observations.append((margin, revenue))
    if not observations:
        return None
    return min(observations, key=lambda observation: observation[0])
