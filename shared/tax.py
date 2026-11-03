"""Canadian sales-tax rates by province (GST/HST/PST), shared by invoicing, bills and GST/HST.

Rates as published by the CRA and provincial ministries; verify before use:
https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/charge-collect-which-rate/calculator.html
PST/QST is shown for completeness but is not recoverable as a GST/HST input tax credit.
"""

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
