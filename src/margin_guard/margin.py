"""Pure margin calculation, independent of storage, Telegram, and network code."""

from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")


def calculate_margin(
    revenue: Decimal,
    cost_price: Decimal | None,
    *,
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
    """Return margin percent rounded to 0.01, or None if cost is unknown.

    ``revenue`` is the per-unit selling price after seller discounts. Each expense
    can be provided as a fixed per-unit amount, a percentage of revenue, or both.
    All monetary values and rates must be Decimal values; rates are percentages
    (for example, ``Decimal("5")`` means five percent).
    """
    if cost_price is None:
        return None
    values = {
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
    for name, value in values.items():
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal")
        if name != "revenue" and value < ZERO:
            raise ValueError(f"{name} cannot be negative")
    if revenue <= ZERO:
        raise ValueError("revenue must be greater than zero")

    fixed_expenses = cost_price + commission + logistics + tax + storage + advertising
    percentage_expenses = revenue * (
        commission_percent
        + logistics_percent
        + tax_percent
        + storage_percent
        + advertising_percent
    ) / HUNDRED
    profit = revenue - fixed_expenses - percentage_expenses
    return (profit / revenue * HUNDRED).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
