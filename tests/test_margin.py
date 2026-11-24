from decimal import Decimal

import pytest

from margin_guard.margin import calculate_margin


def D(value: str) -> Decimal:
    return Decimal(value)


def test_margin_with_cost_only():
    assert calculate_margin(D("1000"), D("600")) == D("40.00")


def test_fixed_expenses_reduce_margin():
    result = calculate_margin(
        D("1000"), D("500"), commission=D("100"), logistics=D("50"), tax=D("30")
    )
    assert result == D("32.00")


def test_percentage_expenses_reduce_margin():
    result = calculate_margin(
        D("200"), D("100"), commission_percent=D("10"), tax_percent=D("5")
    )
    assert result == D("35.00")


def test_fixed_and_percentage_expenses_can_be_combined():
    result = calculate_margin(
        D("100"), D("40"), logistics=D("5"), logistics_percent=D("10")
    )
    assert result == D("45.00")


def test_storage_and_advertising_are_optional_expenses():
    result = calculate_margin(
        D("100"), D("50"), storage=D("5"), advertising_percent=D("5")
    )
    assert result == D("40.00")


def test_loss_is_returned_as_negative_margin():
    assert calculate_margin(D("100"), D("120")) == D("-20.00")


def test_missing_cost_returns_not_calculated():
    assert calculate_margin(D("100"), None) is None


@pytest.mark.parametrize("revenue", [D("0"), D("-0.01")])
def test_zero_or_negative_revenue_is_rejected(revenue):
    with pytest.raises(ValueError, match="revenue"):
        calculate_margin(revenue, D("20"))


def test_margin_rounds_half_up_to_two_decimal_places():
    assert calculate_margin(D("3"), D("2")) == D("33.33")
    assert calculate_margin(D("8"), D("1.9996")) == D("75.01")


def test_float_inputs_are_rejected_to_keep_money_decimal_exact():
    with pytest.raises(TypeError, match="Decimal"):
        calculate_margin(100.0, D("20"))


def test_negative_expense_is_rejected():
    with pytest.raises(ValueError, match="commission"):
        calculate_margin(D("100"), D("20"), commission=D("-1"))
