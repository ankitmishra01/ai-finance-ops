"""Canadian sales-tax rates by province (GST/HST/PST/QST), shared by invoicing, bills and GST/HST.

Rates as published by the CRA and provincial ministries; verify before use:
https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/charge-collect-which-rate/calculator.html
PST, RST and QST are never recoverable as a GST/HST input tax credit. QST has its own input tax
refund (ITR) system with Revenu Quebec. BC PST, SK PST and MB RST paid on ordinary business
purchases are a cost.
"""

import re
from decimal import ROUND_HALF_UP, Decimal

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


# --- Provincial sales taxes (QST, PST, RST) -----------------------------------------------------
# Each is computed on the price before GST (none of the four is charged on the GST) and rounded
# half-up to the cent. Rates checked on 3 November 2026:
#   QC QST 9.975% of the selling price excluding GST; a fraction of a cent of 0.5 or more rounds up.
#     Revenu Quebec, Calculating the Taxes:
#     https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/collecting-gst-and-qst/calculating-the-taxes/
#     Tables of GST and QST Rates:
#     https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/basic-rules-for-applying-the-gsthst-and-qst/tables-of-gst-and-qst-rates/
#     Unsure: revenuquebec.ca refused automated fetches, so the rate and rounding rule above come
#     from the search-engine summary of those pages, not the page text. Open them before relying
#     on this.
#   BC PST 7%: "Generally, the rate of PST is 7% on the purchase or lease price of goods and
#     services, with some exceptions." https://www2.gov.bc.ca/gov/content/taxes/sales-taxes/pst
#     The same page says the planned extension of PST to some professional services is paused.
#   SK PST 6%: "Provincial Sales Tax (PST) is a six per cent sales tax."
#     https://www.saskatchewan.ca/business/taxes-licensing-and-reporting/provincial-taxes-policies-and-bulletins/provincial-sales-tax
#   MB RST 7%: "The general sales tax rate is 7%." https://www.gov.mb.ca/finance/taxation/taxes/retail.html
# These are general rates. What each tax applies to differs: QST follows the GST base closely,
# while BC, SK and MB tax goods and a listed set of services, and some items carry other rates.
# Whether a particular supply is taxable is not decided here.

PROVINCIAL_TAX = {
    # province: (code, rate as an exact decimal string, label)
    "QC": ("QST", "0.09975", "QST 9.975%"),
    "BC": ("PST", "0.07", "PST 7%"),
    "SK": ("PST", "0.06", "PST 6%"),
    "MB": ("RST", "0.07", "RST 7%"),
}


def _pct_cents(subtotal_cents: int, rate: str) -> int:
    """subtotal x rate, half-up to the cent, in Decimal so 9.975% carries no float error."""
    v = Decimal(subtotal_cents) * Decimal(rate)
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def provincial_tax_cents(subtotal_cents: int, province: str) -> int:
    """QST, PST or RST on a pre-GST subtotal; 0 where no separate provincial sales tax applies."""
    p = PROVINCIAL_TAX.get(province)
    return _pct_cents(subtotal_cents, p[1]) if p else 0


def qst_cents(subtotal_cents: int) -> int:
    """Quebec sales tax: 9.975% of the price excluding GST."""
    return provincial_tax_cents(subtotal_cents, "QC")


def bc_pst_cents(subtotal_cents: int) -> int:
    return provincial_tax_cents(subtotal_cents, "BC")


def sk_pst_cents(subtotal_cents: int) -> int:
    return provincial_tax_cents(subtotal_cents, "SK")


def mb_rst_cents(subtotal_cents: int) -> int:
    return provincial_tax_cents(subtotal_cents, "MB")


def provincial_tax_code(province: str | None) -> str | None:
    """'QST', 'PST' or 'RST'; None where no separate provincial sales tax applies."""
    p = PROVINCIAL_TAX.get(province or "")
    return p[0] if p else None


def provincial_tax_label(province: str | None) -> str:
    p = PROVINCIAL_TAX.get(province or "")
    return p[2] if p else ""


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


# --- QST registration numbers -------------------------------------------------------------------
# Format: 10 digits + "TQ" + 4-digit file number, e.g. 1234567890TQ0001 (0001 is the usual file
# number; a business with several divisions can hold TQ0002 and up). Revenu Quebec's own check:
# https://www.revenuquebec.ca/en/online-services/online-services/validating-the-qst-registration-number/
# Unsure: that page could not be fetched while this was written, so the format comes from
# secondary sources that agree with each other (for example Stripe's Quebec sales tax help page).
# No check digit is published, so none is tested. Two legitimate kinds of number fail this check on
# purpose and go to a person: "NR" + 8 digits (non-residents under the specified QST system) and
# a 9-digit GST number used by some financial institutions.

QST_NUMBER_RX = re.compile(r"^\d{10}TQ\d{4}$")


def normalize_qst_number(raw: str | None) -> str | None:
    """Strip spaces and hyphens and upper-case; None for empty input."""
    if not raw:
        return None
    return re.sub(r"[\s\-]", "", raw).upper()


def validate_qst_number(raw: str | None) -> tuple[bool, str]:
    """(ok, reason). Format only; confirm the number with Revenu Quebec before a large refund claim."""
    n = normalize_qst_number(raw)
    if not n:
        return False, "missing"
    if re.fullmatch(r"NR\d{8}", n):
        return False, "NR number (specified QST system); confirm with Revenu Quebec"
    if not QST_NUMBER_RX.match(n):
        return False, "not 10 digits + TQ + 4 digits"
    return True, "ok"


def qst_number_format_ok(raw: str | None) -> bool:
    return validate_qst_number(raw)[0]
