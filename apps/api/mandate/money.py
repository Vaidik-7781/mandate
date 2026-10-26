"""Money handling. Amounts are integers in minor units everywhere except at the PayPal boundary.

PayPal uses decimal strings ("120.00"); Verifiable Intent uses integer minor units. This module is the only
place that converts between the two, and it never touches floats.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ISO 4217 minor-unit exponents for the currencies PayPal supports that differ from 2.
_EXPONENTS: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "CHF": 2, "SEK": 2, "NOK": 2, "DKK": 2,
    "JPY": 0, "HUF": 0, "TWD": 0,
}  # fmt: skip


class MoneyError(ValueError):
    """Raised for any malformed amount."""


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def to_paypal(amount_minor: int, currency: str) -> str:
    """Integer minor units -> PayPal decimal string, e.g. 12000 USD -> '120.00'."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MoneyError(f"amount must be an int of minor units, got {type(amount_minor).__name__}")
    if amount_minor < 0:
        raise MoneyError("amount must not be negative")
    exp = exponent(currency)
    if exp == 0:
        return str(amount_minor)
    scale = 10**exp
    whole, frac = divmod(amount_minor, scale)
    return f"{whole}.{frac:0{exp}d}"


def from_paypal(value: str, currency: str) -> int:
    """PayPal decimal string -> integer minor units. Rejects extra precision and non-strings."""
    if not isinstance(value, str):
        raise MoneyError(f"PayPal amount must be a string, got {type(value).__name__}")
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise MoneyError(f"not a decimal amount: {value!r}") from exc
    if not dec.is_finite() or dec < 0:
        raise MoneyError(f"invalid amount: {value!r}")
    scaled = dec * (10 ** exponent(currency))
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{value!r} has more precision than {currency} allows")
    return int(scaled)


def parse_user_amount(text: str, currency: str = "USD") -> int:
    """Parse a human amount like '$120', '120.50', '1,200' into minor units."""
    cleaned = text.strip().replace(",", "").lstrip("$€£¥").strip()
    return from_paypal(cleaned, currency)


def fmt(amount_minor: int, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), "")
    value = to_paypal(amount_minor, currency)
    return f"{symbol}{value}" if symbol else f"{value} {currency.upper()}"
