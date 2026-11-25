"""Immutable domain value objects and hardened input parsers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .exceptions import DomainValidationError

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")

MAX_DECIMAL_INPUT_LENGTH = 24
MAX_MONEY_AMOUNT = Decimal("1000000000")
MAX_PERCENT_RATE = Decimal("1000")
MIN_THRESHOLD_PERCENT = Decimal("-1000")
MAX_THRESHOLD_PERCENT = Decimal("1000")
MAX_WB_TOKEN_LENGTH = 4096
MAX_PRODUCT_ID_LENGTH = 32

_STRICT_DECIMAL_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_DIGITS_ONLY_RE = re.compile(r"^[1-9]\d{0,31}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class ExpenseCategory(StrEnum):
    """Supported per-unit seller expense categories."""

    COMMISSION = "commission"
    LOGISTICS = "logistics"
    TAX = "tax"
    STORAGE = "storage"
    ADVERTISING = "advertising"


ALL_EXPENSE_CATEGORIES: tuple[ExpenseCategory, ...] = (
    ExpenseCategory.COMMISSION,
    ExpenseCategory.LOGISTICS,
    ExpenseCategory.TAX,
    ExpenseCategory.STORAGE,
    ExpenseCategory.ADVERTISING,
)

REQUIRED_EXPENSE_CATEGORIES: tuple[ExpenseCategory, ...] = (
    ExpenseCategory.COMMISSION,
    ExpenseCategory.LOGISTICS,
    ExpenseCategory.TAX,
)


class MarginState(StrEnum):
    """Persisted state of a product's margin relative to the seller threshold."""

    OK = "ok"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class MarginDecision:
    """Outcome of evaluating a product's current margin against its previous state."""

    state: MarginState | None
    changed: bool
    notify: bool


def sanitize_display_text(value: str | None, *, fallback: str = "?", max_length: int = 100) -> str:
    """Strip control characters and bound untrusted external text for safe display."""
    if not value:
        return fallback
    cleaned = _CONTROL_CHARS_RE.sub(" ", value).strip()
    if not cleaned:
        return fallback
    return cleaned[:max_length]


def validate_product_id(raw: str) -> str:
    """Validate a positive numeric Wildberries nmID string."""
    cleaned = raw.strip()
    if not _DIGITS_ONLY_RE.fullmatch(cleaned):
        raise DomainValidationError(
            "Артикул WB должен быть положительным числом (до 32 цифр)."
        )
    return cleaned


def validate_marketplace_token(raw: str) -> str:
    """Validate that a marketplace token is non-empty printable ASCII without whitespace.

    Rejects control characters, CRLF header-injection sequences, non-ASCII characters
    (which would fail HTTP header encoding), and oversized payloads.
    """
    token = raw.strip()
    if not token:
        raise DomainValidationError("Токен WB API не должен быть пустым.")
    if len(token) > MAX_WB_TOKEN_LENGTH:
        raise DomainValidationError("Токен WB API превышает допустимую длину.")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
        raise DomainValidationError(
            "Токен WB API должен содержать только печатные ASCII-символы без пробелов."
        )
    return token


def parse_bounded_decimal(
    raw: str,
    *,
    field_label: str,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
    max_scale: int = 4,
) -> Decimal:
    """Parse a user-supplied decimal safely, blocking scientific-notation memory DoS.

    ``Decimal("1e99999999").is_finite()`` is True in Python, and ``format(..., "f")``
    would allocate hundreds of megabytes. Matching strict fixed-point digits and
    bounding length/scale eliminates this entire class of attacks.
    """
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > MAX_DECIMAL_INPUT_LENGTH:
        raise DomainValidationError(f"Некорректное числовое значение для поля «{field_label}».")
    if not _STRICT_DECIMAL_RE.fullmatch(cleaned):
        raise DomainValidationError(
            f"Поле «{field_label}» должно быть обычным числом (например, 125.50)."
        )
    normalized = cleaned.replace(",", ".")
    if "." in normalized:
        _, fractional = normalized.split(".", maxsplit=1)
        if len(fractional) > max_scale:
            raise DomainValidationError(
                f"Поле «{field_label}» поддерживает не более {max_scale} знаков после запятой."
            )
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise DomainValidationError(f"Некорректное число для поля «{field_label}».") from exc

    if not value.is_finite():  # pragma: no cover - guarded by regex
        raise DomainValidationError(f"Поле «{field_label}» должно быть конечным числом.")
    if value == ZERO:
        value = ZERO
    if min_value is not None and value < min_value:
        raise DomainValidationError(
            f"Поле «{field_label}» должно быть не меньше {format_decimal(min_value)}."
        )
    if max_value is not None and value > max_value:
        raise DomainValidationError(
            f"Поле «{field_label}» должно быть не больше {format_decimal(max_value)}."
        )
    return value


def format_decimal(value: Decimal) -> str:
    """Format a finite bounded Decimal without scientific notation or trailing noise."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainValidationError("Ожидалось конечное число Decimal.")
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int) or abs(exponent) + len(digits) > 64:
        raise DomainValidationError("Число выходит за допустимые пределы точности.")
    normalized = ZERO if value == ZERO else value
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class ExpenseRule:
    """Per-unit expense expressed as a fixed amount, percentage of revenue, or both."""

    fixed: Decimal = ZERO
    percent: Decimal = ZERO

    def __post_init__(self) -> None:
        if not isinstance(self.fixed, Decimal) or not isinstance(self.percent, Decimal):
            raise TypeError("ExpenseRule values must be Decimal instances")
        if not self.fixed.is_finite() or not self.percent.is_finite():
            raise ValueError("ExpenseRule values must be finite")
        if self.fixed < ZERO or self.percent < ZERO:
            raise ValueError("ExpenseRule values cannot be negative")
        if self.fixed > MAX_MONEY_AMOUNT:
            raise ValueError("ExpenseRule fixed value exceeds maximum bound")
        if self.percent > MAX_PERCENT_RATE:
            raise ValueError("ExpenseRule percent value exceeds maximum bound")

    def amount_for_revenue(self, revenue: Decimal) -> Decimal:
        """Compute total per-unit expense for a given selling price."""
        return self.fixed + (revenue * self.percent / HUNDRED)


@dataclass(frozen=True, slots=True)
class ExpenseSheet:
    """Complete resolved set of per-unit expenses ready for margin calculation."""

    commission: ExpenseRule = ExpenseRule()
    logistics: ExpenseRule = ExpenseRule()
    tax: ExpenseRule = ExpenseRule()
    storage: ExpenseRule = ExpenseRule()
    advertising: ExpenseRule = ExpenseRule()

    @classmethod
    def resolve(
        cls,
        defaults: Mapping[ExpenseCategory, ExpenseRule],
        overrides: Mapping[ExpenseCategory, ExpenseRule],
    ) -> ExpenseSheet | None:
        """Merge user defaults with product overrides; return None if required fees are unset."""
        resolved: dict[ExpenseCategory, ExpenseRule] = {}
        for category in ALL_EXPENSE_CATEGORIES:
            rule = overrides.get(category, defaults.get(category))
            if rule is None:
                if category in REQUIRED_EXPENSE_CATEGORIES:
                    return None
                rule = ExpenseRule()
            resolved[category] = rule
        return cls(
            commission=resolved[ExpenseCategory.COMMISSION],
            logistics=resolved[ExpenseCategory.LOGISTICS],
            tax=resolved[ExpenseCategory.TAX],
            storage=resolved[ExpenseCategory.STORAGE],
            advertising=resolved[ExpenseCategory.ADVERTISING],
        )

    def total_for_revenue(self, revenue: Decimal) -> Decimal:
        """Sum fixed and percentage expenses across all five categories."""
        return (
            self.commission.amount_for_revenue(revenue)
            + self.logistics.amount_for_revenue(revenue)
            + self.tax.amount_for_revenue(revenue)
            + self.storage.amount_for_revenue(revenue)
            + self.advertising.amount_for_revenue(revenue)
        )


def parse_fee_assignments(
    assignments: Sequence[str],
    *,
    allow_reset_to_default: bool,
) -> dict[ExpenseCategory, ExpenseRule | None]:
    """Parse CLI-style expense assignments such as ``commission=10%`` or ``logistics=50+5%``."""
    if not assignments:
        raise DomainValidationError("Укажите хотя бы один расход в формате имя=значение.")
    if len(assignments) > len(ALL_EXPENSE_CATEGORIES):
        raise DomainValidationError("Слишком много параметров расходов в одной команде.")

    allowed_by_name = {category.value: category for category in ALL_EXPENSE_CATEGORIES}
    parsed: dict[ExpenseCategory, ExpenseRule | None] = {}

    for assignment in assignments:
        if len(assignment) > 64 or "=" not in assignment:
            raise DomainValidationError(
                "Расходы задаются как имя=значение, например commission=10%."
            )
        raw_name, raw_value = assignment.split("=", maxsplit=1)
        category = allowed_by_name.get(raw_name.strip())
        if category is None:
            raise DomainValidationError(
                "Допустимые расходы: commission, logistics, tax, storage, advertising."
            )
        if category in parsed:
            raise DomainValidationError(f"Расход {category.value} указан несколько раз.")

        value_text = raw_value.strip()
        if value_text == "default":
            if not allow_reset_to_default:
                raise DomainValidationError(
                    "Значение default допустимо только для индивидуальных расходов товара."
                )
            parsed[category] = None
            continue

        fixed = ZERO
        percent = ZERO
        try:
            if "+" in value_text:
                fixed_part, percent_part = value_text.split("+", maxsplit=1)
                if not percent_part.endswith("%"):
                    raise DomainValidationError("Процентная часть должна заканчиваться знаком %.")
                fixed = parse_bounded_decimal(
                    fixed_part,
                    field_label=category.value,
                    min_value=ZERO,
                    max_value=MAX_MONEY_AMOUNT,
                )
                percent = parse_bounded_decimal(
                    percent_part[:-1],
                    field_label=f"{category.value}%",
                    min_value=ZERO,
                    max_value=MAX_PERCENT_RATE,
                )
            elif value_text.endswith("%"):
                percent = parse_bounded_decimal(
                    value_text[:-1],
                    field_label=f"{category.value}%",
                    min_value=ZERO,
                    max_value=MAX_PERCENT_RATE,
                )
            else:
                fixed = parse_bounded_decimal(
                    value_text,
                    field_label=category.value,
                    min_value=ZERO,
                    max_value=MAX_MONEY_AMOUNT,
                )
        except DomainValidationError as exc:
            raise DomainValidationError(
                f"Некорректное значение для {category.value}: используйте сумму (50), "
                f"процент (6%) или сумму+процент (50+6%) в допустимых пределах."
            ) from exc

        parsed[category] = ExpenseRule(fixed=fixed, percent=percent)

    return parsed
