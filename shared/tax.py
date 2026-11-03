"""Canadian sales-tax rates by province (GST/HST/PST), shared by invoicing, bills and GST/HST.

Rates as published by the CRA and provincial ministries; verify before use:
https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/charge-collect-which-rate/calculator.html
PST/QST is shown for completeness but is not recoverable as a GST/HST input tax credit.
"""

import re

RATES = {
    # province: (gst_or_hst_rate, pst_rate, label)
    "ON": (0.13, 0.0, "HST 13%"),
    "NS": (0.14, 0.0, "HST 14%"),
    "NB": (0.15, 0.0, "HST 15%"),
    "NL": (0.15, 0.0, "HST 15%"),
    "PE": (0.15, 0.0, "HST 15%"),
    "AB": (0.05, 0.0, "GST 5%"),
    "BC": (0.05, 0.07, "GST 5% + PST 7%"),
    "SK": (0.05, 0.06, "GST 5% + PST 6%"),
    "MB": (0.05, 0.07, "GST 5% + RST 7%"),
    "QC": (0.05, 0.09975, "GST 5% + QST 9.975%"),
    "YT": (0.05, 0.0, "GST 5%"),
    "NT": (0.05, 0.0, "GST 5%"),
    "NU": (0.05, 0.0, "GST 5%"),
}


def gst_hst_cents(subtotal_cents: int, province: str) -> int:
    """GST/HST only (the part the supplier remits to the CRA), rounded half-up to the cent."""
    rate = RATES[province][0]
    return int(subtotal_cents * rate + 0.5)


def cents(x: float) -> int:
    return int(round(x * 100))


def fmt(c: int) -> str:
    return f"${c / 100:,.2f}"


# --- GST/HST registration numbers ---------------------------------------------------------------
# Format: 9-digit business number (BN) + program identifier "RT" + 4-digit reference number,
# e.g. 123456782RT0001. The 9th BN digit is a Luhn mod-10 check digit over the first eight.
# The CRA does not publish the check digit as a taxpayer-facing rule, so a failure is a prompt to
# confirm the number in the CRA GST/HST Registry, not proof that it is invalid.
# https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/registering-your-business/business-number.html

GST_HST_NUMBER_RX = re.compile(r"^\d{9}RT\d{4}$")


def normalize_gst_hst_number(raw: str | None) -> str | None:
    """Strip spaces and hyphens and upper-case; None for empty input."""
    if not raw:
        return None
    return re.sub(r"[\s\-]", "", raw).upper()


def bn_check_digit(first_eight: str) -> int:
    """Luhn check digit for the first eight digits of a business number."""
    total = 0
    for i, ch in enumerate(reversed(first_eight)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10


def gst_hst_number_format_ok(raw: str | None) -> bool:
    n = normalize_gst_hst_number(raw)
    return bool(n and GST_HST_NUMBER_RX.match(n))


def gst_hst_number_check_digit_ok(raw: str | None) -> bool:
    """Format is right and the 9th BN digit matches the Luhn check digit of the first eight."""
    n = normalize_gst_hst_number(raw)
    return bool(n and GST_HST_NUMBER_RX.match(n) and int(n[8]) == bn_check_digit(n[:8]))


def validate_gst_hst_number(raw: str | None) -> tuple[bool, str]:
    """(ok, reason). ok is True only when format and check digit both pass."""
    n = normalize_gst_hst_number(raw)
    if not n:
        return False, "missing"
    if not GST_HST_NUMBER_RX.match(n):
        return False, "not 9 digits + RT + 4 digits"
    if int(n[8]) != bn_check_digit(n[:8]):
        return False, "fails the BN check digit"
    return True, "ok"
