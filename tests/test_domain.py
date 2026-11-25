"""Unit tests for Domain entities, value objects, parsers, and services."""

from __future__ import annotations

from decimal import Decimal

import pytest

from margin_guard.domain.entities import CatalogProduct, ProductSizePrice, StoredProduct
from margin_guard.domain.exceptions import DomainValidationError
from margin_guard.domain.services import (
    calculate_margin,
    evaluate_margin_state,
    evaluate_product_margin,
)
from margin_guard.domain.value_objects import (
    ExpenseCategory,
    ExpenseRule,
    ExpenseSheet,
    MarginState,
    format_decimal,
    parse_bounded_decimal,
    parse_fee_assignments,
    sanitize_display_text,
    validate_marketplace_token,
    validate_product_id,
)


def D(value: str) -> Decimal:
    return Decimal(value)


def test_margin_with_cost_only() -> None:
    assert calculate_margin(D("1000"), D("600")) == D("40.00")


def test_fixed_and_percentage_expenses_reduce_margin() -> None:
    assert (
        calculate_margin(
            D("1000"),
            D("500"),
            commission=D("100"),
            logistics=D("50"),
            tax=D("30"),
        )
        == D("32.00")
    )
    assert (
        calculate_margin(
            D("200"),
            D("100"),
            commission_percent=D("10"),
            tax_percent=D("5"),
        )
        == D("35.00")
    )
    assert (
        calculate_margin(
            D("100"),
            D("40"),
            logistics=D("5"),
            logistics_percent=D("10"),
            storage=D("2"),
            advertising_percent=D("3"),
        )
        == D("40.00")
    )


def test_loss_and_missing_cost_and_half_up_rounding() -> None:
    assert calculate_margin(D("100"), D("120")) == D("-20.00")
    assert calculate_margin(D("100"), None) is None
    assert calculate_margin(D("3"), D("2")) == D("33.33")
    assert calculate_margin(D("8"), D("1.9996")) == D("75.01")


@pytest.mark.parametrize("revenue", [D("0"), D("-0.01")])
def test_zero_or_negative_revenue_is_rejected(revenue: Decimal) -> None:
    with pytest.raises(ValueError, match="revenue"):
        calculate_margin(revenue, D("20"))


def test_float_nan_and_negative_expenses_are_rejected() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        calculate_margin(100.0, D("20"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        calculate_margin(D("NaN"), D("20"))
    with pytest.raises(ValueError, match="commission"):
        calculate_margin(D("100"), D("20"), commission=D("-1"))


@pytest.mark.parametrize(
    "payload",
    [
        "1e99999999",
        "1E5000",
        "NaN",
        "Infinity",
        "-Infinity",
        "9" * 30,
        "12.3456789",
        "",
        "abc",
    ],
)
def test_parse_bounded_decimal_blocks_scientific_notation_dos_and_malformed_inputs(
    payload: str,
) -> None:
    with pytest.raises(DomainValidationError):
        parse_bounded_decimal(payload, field_label="тест")


def test_parse_bounded_decimal_accepts_comma_and_enforces_range() -> None:
    assert parse_bounded_decimal("125,50", field_label="цена", min_value=D("0")) == D("125.50")
    assert format_decimal(D("-0.00")) == "0"
    with pytest.raises(DomainValidationError, match="не меньше"):
        parse_bounded_decimal("-1", field_label="цена", min_value=D("0"))
    with pytest.raises(DomainValidationError, match="не больше"):
        parse_bounded_decimal("101", field_label="ставка", max_value=D("100"))


@pytest.mark.parametrize(
    "bad_token",
    [
        "",
        "   ",
        "привет-токен",
        "token with space",
        "token\r\nInjected: header",
        "token\x00null",
        "a" * 5000,
    ],
)
def test_validate_marketplace_token_rejects_unsafe_tokens(bad_token: str) -> None:
    with pytest.raises(DomainValidationError):
        validate_marketplace_token(bad_token)


def test_validate_product_id_and_sanitize_display_text() -> None:
    assert validate_product_id(" 123456 ") == "123456"
    with pytest.raises(DomainValidationError):
        validate_product_id("0")
    with pytest.raises(DomainValidationError):
        validate_product_id("12ab")
    assert sanitize_display_text("Line1\r\n\x00Line2") == "Line1 Line2"


def test_parse_fee_assignments_handles_all_syntaxes_and_guards_errors() -> None:
    parsed = parse_fee_assignments(
        ["commission=10%", "logistics=50+5%", "tax=6%", "storage=15"],
        allow_reset_to_default=False,
    )
    assert parsed[ExpenseCategory.COMMISSION] == ExpenseRule(D("0"), D("10"))
    assert parsed[ExpenseCategory.LOGISTICS] == ExpenseRule(D("50"), D("5"))

    reset = parse_fee_assignments(["commission=default"], allow_reset_to_default=True)
    assert reset[ExpenseCategory.COMMISSION] is None

    with pytest.raises(DomainValidationError, match="индивидуальных"):
        parse_fee_assignments(["commission=default"], allow_reset_to_default=False)
    with pytest.raises(DomainValidationError, match="несколько раз"):
        parse_fee_assignments(
            ["tax=6%", "tax=7%"], allow_reset_to_default=False
        )
    with pytest.raises(DomainValidationError):
        parse_fee_assignments(["commission=1e999999%"], allow_reset_to_default=False)


def test_evaluate_margin_state_transitions() -> None:
    alert = evaluate_margin_state(D("9.99"), D("10"), MarginState.OK)
    assert (alert.state, alert.changed, alert.notify) == (MarginState.BELOW_THRESHOLD, True, True)

    steady_low = evaluate_margin_state(D("5"), D("10"), MarginState.BELOW_THRESHOLD)
    assert (steady_low.state, steady_low.changed, steady_low.notify) == (
        MarginState.BELOW_THRESHOLD,
        False,
        False,
    )

    recovered = evaluate_margin_state(D("10"), D("10"), MarginState.BELOW_THRESHOLD)
    assert (recovered.state, recovered.changed, recovered.notify) == (
        MarginState.OK,
        True,
        False,
    )

    initial = evaluate_margin_state(D("2"), D("10"), None)
    assert (initial.state, initial.changed, initial.notify) == (
        MarginState.BELOW_THRESHOLD,
        True,
        False,
    )

    unknown = evaluate_margin_state(None, D("10"), MarginState.OK)
    assert (unknown.state, unknown.changed, unknown.notify) == (MarginState.OK, False, False)


def test_evaluate_product_margin_selects_lowest_size_and_merges_overrides() -> None:
    catalog = CatalogProduct(
        nm_id="101",
        title="Sneakers",
        sizes=(
            ProductSizePrice(price=D("150"), discounted_price=D("150")),
            ProductSizePrice(price=D("100"), discounted_price=D("80")),
        ),
    )
    stored = StoredProduct(
        nm_id="101",
        cost_price=D("50"),
        expense_overrides={ExpenseCategory.LOGISTICS: ExpenseRule(fixed=D("5"))},
    )
    defaults = {
        ExpenseCategory.COMMISSION: ExpenseRule(percent=D("10")),
        ExpenseCategory.LOGISTICS: ExpenseRule(fixed=D("20")),
        ExpenseCategory.TAX: ExpenseRule(percent=D("5")),
    }
    result = evaluate_product_margin(catalog, stored, defaults)
    assert result == (D("16.25"), D("80"))

    missing_tax = {ExpenseCategory.COMMISSION: ExpenseRule(percent=D("10"))}
    assert ExpenseSheet.resolve(missing_tax, {}) is None
    assert evaluate_product_margin(catalog, stored, missing_tax) is None
