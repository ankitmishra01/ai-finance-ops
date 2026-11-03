"""Seeded generator for the bookkeeping module's synthetic month (October 2026).

Writes to data/synthetic/bookkeeping/:
  transactions.csv        bank (CHQ-1001) and Visa (VISA-4421) lines, messy descriptors
  ledger_current.csv      the category each transaction currently has in the books
                          (some deliberately wrong, some uncategorised)
  chart_of_accounts.yaml  accounts with generic keywords for the rules baseline
  mailbox/*.eml           receipts and invoices, some with PDF attachments
  labels.json             gold labels: receipt fields, receipt->transaction links,
                          transaction categories, expected flags

Everything is fictional: merchant names are invented, domains end in .test, and
people's names come from Faker (en_CA, seed 42).

Run:  uv run python 05_bookkeeping/bookkeeping_data.py
"""

from __future__ import annotations

import csv
import io
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from faker import Faker  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from shared.mail import build_eml  # noqa: E402
from shared.tax import gst_hst_cents  # noqa: E402

OUT = ROOT / "data" / "synthetic" / "bookkeeping"
SEED = 42
YEAR, MONTH = 2026, 10
BOOKS = "books@larchmont.test"
BUSINESS = "Larchmont Design Co."
CARD = "VISA-4421"
BANK = "CHQ-1001"

CHART = {
    "entity": {"name": BUSINESS, "province": "ON", "currency": "CAD", "fiscal_year_end": "12-31"},
    "fallback": "9999",
    "accounts": [
        {"code": "1500", "name": "Computer Equipment (capital)", "type": "asset", "min_amount": 500,
         "description": "Computers, monitors and hardware costing $500 or more per item; capitalised, not expensed.",
         "keywords": ["laptop", "computer", "notebook pc", "workstation"]},
        {"code": "2100", "name": "Visa Payable (card payments)", "type": "liability", "evidence_required": False,
         "description": "Payments from the bank account to the business credit card. Transfer, not an expense.",
         "keywords": ["payment - thank you", "pmt visa", "visa payment"]},
        {"code": "2200", "name": "GST/HST Payable", "type": "liability", "evidence_required": False,
         "description": "GST/HST remittances to the CRA.",
         "keywords": ["govt canada", "gst34", "gst/hst payment", "receiver general"]},
        {"code": "3100", "name": "Owner's Draws", "type": "equity", "evidence_required": False,
         "description": "Money taken out by the owner.",
         "keywords": ["owner draw"]},
        {"code": "4010", "name": "Sales - Online (card processor payouts)", "type": "income", "evidence_required": False,
         "description": "Payouts from the online card processor.",
         "keywords": ["stripe transfer", "stripe payout"]},
        {"code": "4020", "name": "Sales - Client Payments", "type": "income", "evidence_required": False,
         "description": "EFT and cheque deposits from clients.",
         "keywords": ["eft credit", "mobile deposit", "deposit"]},
        {"code": "5010", "name": "Advertising & Marketing", "type": "expense",
         "description": "Online ads, printing of marketing material, business cards.",
         "keywords": ["ads", "advertising", "campaign", "business cards", "flyers", "print shop", "promotion"]},
        {"code": "5020", "name": "Bank Charges & Fees", "type": "expense", "evidence_required": False,
         "description": "Account fees, transfer fees, interest.",
         "keywords": ["account fee", "e-trf fee", "service charge", "interest charge", "monthly fee",
                      "annual fee"]},
        {"code": "5030", "name": "Meals & Entertainment", "type": "expense",
         "description": "Restaurant meals, coffee and food delivery for business purposes (50% deductible).",
         "keywords": ["cafe", "coffee", "bistro", "bakery", "grill", "kitchen", "restaurant", "latte",
                      "lunch", "dinner", "espresso", "sandwich", "beans", "croissant"]},
        {"code": "5040", "name": "Office Supplies & Small Equipment", "type": "expense",
         "description": "Stationery, paper, toner and small items under $500.",
         "keywords": ["office", "paper", "toner", "stationery", "pens", "notebooks", "desk", "lamp",
                      "stand", "monitor arm", "cable", "mktp"]},
        {"code": "5050", "name": "Software & Subscriptions", "type": "expense",
         "description": "SaaS, cloud storage and software licences.",
         "keywords": ["subscription", "subscr", "software", "saas", "plan", "licence", "license",
                      "cloud", "storage", "workspace", "seat"]},
        {"code": "5060", "name": "Telephone & Internet", "type": "expense",
         "description": "Mobile phone and internet service.",
         "keywords": ["mobile", "wireless", "fibre", "internet", "phone"]},
        {"code": "5070", "name": "Travel", "type": "expense",
         "description": "Rail, air, hotel, taxi and rideshare while travelling or between client sites.",
         "keywords": ["rail", "train", "hotel", "suites", "taxi", "trip", "ride", "air", "fare", "flight"]},
        {"code": "5080", "name": "Vehicle - Fuel & Parking", "type": "expense",
         "description": "Fuel and parking for the business vehicle.",
         "keywords": ["fuel", "gas", "parking", "park lot", "petrol", "litres"]},
        {"code": "5090", "name": "Professional & Contractor Fees", "type": "expense",
         "description": "Accountants, lawyers, freelance designers and other contractors.",
         "keywords": ["cpa", "accounting services", "legal", "consulting", "design services",
                      "contractor", "bookkeeping services", "professional"]},
        {"code": "5100", "name": "Rent", "type": "expense",
         "description": "Office or studio rent.",
         "keywords": ["rent", "lease", "properties"]},
        {"code": "5110", "name": "Utilities", "type": "expense",
         "description": "Hydro, gas and water for the premises.",
         "keywords": ["hydro", "electricity", "utility", "water"]},
        {"code": "5120", "name": "Insurance", "type": "expense",
         "description": "Business insurance premiums.",
         "keywords": ["insurance", "ins ", "premium", "mutual"]},
        {"code": "5130", "name": "Shipping & Postage", "type": "expense",
         "description": "Couriers and postage.",
         "keywords": ["parcel", "shipping", "postage", "courier"]},
        {"code": "5140", "name": "Training & Professional Development", "type": "expense",
         "description": "Courses, workshops and conferences.",
         "keywords": ["course", "courses", "training", "workshop", "certificate"]},
        {"code": "9999", "name": "Uncategorised - Needs Review", "type": "suspense",
         "description": "Holding account for transactions nobody has categorised yet.",
         "keywords": []},
    ],
}


# ---------------------------------------------------------------- helpers

def d(day: int, month: int = MONTH, year: int = YEAR) -> date:
    return date(year, month, day)


def money(c: int) -> str:
    sign = "-" if c < 0 else ""
    return f"{sign}${abs(c) / 100:,.2f}"


def eml_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S -0400")


def long_date(x: date) -> str:
    return f"{x.strftime('%B')} {x.day}, {x.year}"


def short_date(x: date) -> str:
    return f"{x.strftime('%b')} {x.day}, {x.year}"


def gst_no(rng: random.Random) -> str:
    return f"{rng.randint(10000, 99999)} {rng.randint(1000, 9999)} RT0001"


def pdf(lines: list[tuple[str, str]], title: str) -> bytes:
    """Render (left, right) text rows into a one-page PDF. invariant=1 keeps output byte-stable."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    c.setTitle(title)
    y = 740
    for i, (left, right) in enumerate(lines):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 14 if i == 0 else 10)
        c.drawString(60, y, left)
        if right:
            c.drawRightString(550, y, right)
        y -= 22 if i == 0 else 15
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- receipt templates
# Each returns (sender, subject, body, attachments, email_datetime)

def tpl_tillpoint(r, rng):
    lines = [f"Receipt from {r['merchant']}", r["address"], f"{short_date(r['date'])} at {r['time']}", ""]
    for desc, amt in r["lines"]:
        lines.append(f"{desc:<28}${amt / 100:,.2f}")
    lines += ["", f"{'Subtotal':<28}${r['subtotal'] / 100:,.2f}", f"{'HST 13%':<28}${r['gst'] / 100:,.2f}"]
    if r.get("tip_on_receipt"):
        lines.append(f"{'Tip':<28}${r['tip_on_receipt'] / 100:,.2f}")
    lines += [f"{'Total':<28}${r['total'] / 100:,.2f}", "", "Paid with Visa .... 4421"]
    if r.get("gst_number"):
        lines.append(f"GST/HST #: {r['gst_number']}")
    lines += ["", "Powered by Tillpoint. Questions? Reply to this email."]
    sender = f"{r['merchant']} via Tillpoint <receipts@tillpoint.test>"
    return sender, f"Your receipt from {r['merchant']}", "\n".join(lines), [], r["dt"]


def tpl_pdf_invoice(r, rng):
    rows = [(r["merchant"], ""), (r["address"], ""), ("", ""), (r.get("doc_title", "INVOICE"), ""),
            (f"Invoice #: {r['invoice_no']}", ""), (f"Invoice date: {r['date'].isoformat()}", ""),
            (f"Bill to: {BUSINESS}", ""), ("", ""), ("Description", "Amount")]
    rows += [(desc, f"${amt / 100:,.2f}") for desc, amt in r["lines"]]
    rows += [("", ""), ("Subtotal", f"${r['subtotal'] / 100:,.2f}")]
    if r["gst"]:
        rows.append(("HST (13%)", f"${r['gst'] / 100:,.2f}"))
    if r.get("other_tax"):
        rows.append((r["other_tax_label"], f"${r['other_tax'] / 100:,.2f}"))
    rows.append(("Total due (CAD)", f"${r['total'] / 100:,.2f}"))
    rows.append(("", ""))
    rows.append((r.get("terms", "Terms: due on receipt"), ""))
    if r.get("gst_number"):
        rows.append((f"GST/HST Reg. No. {r['gst_number']}", ""))
    fname = f"{r['invoice_no']}.pdf"
    body = (f"Hello,\n\nPlease find attached {r.get('doc_title', 'invoice').lower()} {r['invoice_no']} "
            f"from {r['merchant']}.\n\nThank you for your business.\n{r['merchant']} Accounts")
    sender = f"{r['merchant']} Accounts <billing@{r['domain']}>"
    subject = f"{r.get('doc_title', 'Invoice').title()} {r['invoice_no']} from {r['merchant']}"
    return sender, subject, body, [(fname, "application/pdf", pdf(rows, r["invoice_no"]))], r["dt"]


def tpl_saas(r, rng):
    cur = "US$" if r["currency"] == "USD" else "$"
    lines = [f"Thanks for your payment to {r['merchant']}.", "",
             f"Amount paid: {cur}{r['total'] / 100:,.2f}", f"Date paid: {long_date(r['date'])}",
             "Payment method: Visa ending in 4421", f"Receipt number: {r['invoice_no']}", "", "Summary"]
    for desc, amt in r["lines"]:
        lines.append(f"{desc}    {cur}{amt / 100:,.2f}")
    lines += [f"Subtotal    {cur}{r['subtotal'] / 100:,.2f}", f"Tax    {cur}{r['gst'] / 100:,.2f}",
              f"Amount paid    {cur}{r['total'] / 100:,.2f}"]
    if r.get("gst_number"):
        lines.append(f"GST/HST registration: {r['gst_number']}")
    sender = f"{r['merchant']} Billing <billing@{r['domain']}>"
    return sender, f"Your {r['merchant']} receipt [{r['invoice_no']}]", "\n".join(lines), [], r["dt"]


def tpl_order(r, rng):
    lines = [f"Hello,", "", "Thanks for your order. Here is your order confirmation.", "",
             f"Order #{r['invoice_no']}", f"Order placed: {long_date(r['date'])}", ""]
    for desc, amt in r["lines"]:
        lines.append(f"  {desc}  CDN$ {amt / 100:,.2f}")
    lines += ["", f"Items subtotal: CDN$ {r['subtotal'] / 100:,.2f}", f"Estimated HST: CDN$ {r['gst'] / 100:,.2f}",
              f"Order total: CDN$ {r['total'] / 100:,.2f}", "",
              f"Sold by {r['merchant']} Canada Ltd."]
    if r.get("gst_number"):
        lines.append(f"GST/HST {r['gst_number']}")
    sender = f"{r['merchant']} <order-update@{r['domain']}>"
    return sender, f"Your {r['merchant']} order #{r['invoice_no']}", "\n".join(lines), [], r["dt"]


def tpl_refund(r, rng):
    lines = ["Your refund has been processed.", "",
             f"Refund for order #{r['invoice_no']}", f"Item: {r['lines'][0][0]}",
             f"Refund date: {long_date(r['date'])}", "",
             f"Refund subtotal: CDN$ {r['subtotal'] / 100:,.2f}", f"HST refunded: CDN$ {r['gst'] / 100:,.2f}",
             f"Total refund: CDN$ {r['total'] / 100:,.2f}", "",
             "The refund will appear on your Visa ending in 4421 within 3-5 business days."]
    if r.get("gst_number"):
        lines.append(f"GST/HST {r['gst_number']}")
    sender = f"{r['merchant']} <returns@{r['domain']}>"
    return sender, f"Refund issued for your {r['merchant']} order", "\n".join(lines), [], r["dt"]


def tpl_ride(r, rng):
    lines = [f"Thanks for riding, {r['rider']}", long_date(r["date"]), "",
             f"Total    ${r['total'] / 100:,.2f}", ""]
    for desc, amt in r["lines"]:
        lines.append(f"{desc}    ${amt / 100:,.2f}")
    lines += [f"HST    ${r['gst'] / 100:,.2f}", "", f"Pickup: {r['pickup']}", f"Dropoff: {r['dropoff']}", "",
              "Charged to Visa ....4421"]
    if r.get("gst_number"):
        lines.append(f"ZipRide Canada GST/HST: {r['gst_number']}")
    return "ZipRide Receipts <noreply@zipride.test>", f"Your {r['when']} trip with ZipRide", "\n".join(lines), [], r["dt"]


def tpl_folio(r, rng):
    dep = r["date"]
    arr = dep - timedelta(days=1)
    rows = [(r["merchant"], ""), (r["address"], ""), ("", ""), ("GUEST FOLIO", ""),
            (f"Folio: {r['invoice_no']}", ""), (f"Guest: {r['guest']}  /  {BUSINESS}", ""),
            (f"Arrival: {arr.strftime('%d/%m/%Y')}", f"Departure: {dep.strftime('%d/%m/%Y')}"), ("", "")]
    rows += [(desc, f"{amt / 100:,.2f}") for desc, amt in r["lines"]]
    rows += [("HST 13%", f"{r['gst'] / 100:,.2f}"), ("Municipal Accommodation Tax 4%", f"{r['other_tax'] / 100:,.2f}"),
             ("Total charges", f"{r['total'] / 100:,.2f}"), ("Paid: VISA XXXX4421", f"{r['total'] / 100:,.2f}"),
             ("Balance", "0.00"), ("", ""), (f"HST Registration {r['gst_number']}", "")]
    body = f"Dear guest,\n\nThank you for staying with us. Your folio is attached.\n\n{r['merchant']}"
    return (f"{r['merchant']} <frontdesk@{r['domain']}>", f"Your folio from {r['merchant']}", body,
            [(f"folio_{r['invoice_no']}.pdf", "application/pdf", pdf(rows, r["invoice_no"]))], r["dt"])


def tpl_plain(r, rng):
    lines = [r["merchant"].upper(), r["address"], f"Date: {r['date'].isoformat()}  {r['time']}", ""]
    for desc, amt in r["lines"]:
        lines.append(f"{desc:<26}{amt / 100:>9.2f}")
    lines += [f"{'SUBTOTAL':<26}{r['subtotal'] / 100:>9.2f}", f"{'HST':<26}{r['gst'] / 100:>9.2f}",
              f"{'TOTAL':<26}{r['total'] / 100:>9.2f}", "", "VISA ************4421   APPROVED"]
    if r.get("gst_number"):
        lines.append(f"HST# {r['gst_number']}")
    body = "Here is your e-receipt.\n\n" + "\n".join(lines)
    return (f"{r['merchant']} <ereceipt@{r['domain']}>", f"E-receipt - {r['merchant']}", body, [], r["dt"])


def tpl_fr(r, rng):
    """French-language Quebec receipt: comma decimals, TPS/TVQ, French month names."""
    fr = lambda c: f"{c / 100:,.2f}".replace(",", " ").replace(".", ",") + " $"  # noqa: E731
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
            "novembre", "décembre"][r["date"].month - 1]
    lines = [r["merchant"], r["address"], f"Date : {r['date'].day} {mois} {r['date'].year}", "", "Reçu de caisse", ""]
    lines += [f"{desc:<30}{fr(amt):>12}" for desc, amt in r["lines"]]
    lines += ["", f"{'Sous-total':<30}{fr(r['subtotal']):>12}", f"{'TPS 5 %':<30}{fr(r['gst']):>12}",
              f"{'TVQ 9,975 %':<30}{fr(r['other_tax']):>12}", f"{'Total':<30}{fr(r['total']):>12}", "",
              f"No TPS : {r['gst_number']}", f"No TVQ : {r['qst_number']}", "Merci et à bientôt !"]
    return (f"{r['merchant']} <recus@{r['domain']}>", f"Votre reçu - {r['merchant']}", "\n".join(lines), [], r["dt"])


TEMPLATES = {"tillpoint": tpl_tillpoint, "pdf": tpl_pdf_invoice, "saas": tpl_saas, "order": tpl_order,
             "refund": tpl_refund, "ride": tpl_ride, "folio": tpl_folio, "plain": tpl_plain,
             "fr": tpl_fr}


# ---------------------------------------------------------------- the month

def build_month(fake: Faker, rng: random.Random):
    """Returns (transactions, receipts). Each transaction carries its gold category, its current
    (possibly wrong) ledger category, and the key of the receipt that evidences it, if any."""
    owner = fake.name()
    contractor = (fake.first_name(), fake.last_name())
    city_addr = lambda: f"{fake.street_address()}, Toronto, ON {fake.postalcode_in_province('ON')}"  # noqa: E731

    txns: list[dict] = []
    receipts: dict[str, dict] = {}

    def receipt(key, *, merchant, template, rdate, lines, domain, hst=True, gst_number=None, currency="CAD",
                lag_email=0, refund=False, tip=0, province="ON", **extra):
        subtotal = sum(a for _, a in lines)
        gst = gst_hst_cents(subtotal, province) if (hst and currency == "CAD") else 0
        other = extra.get("other_tax", 0)
        total = subtotal + gst + other + tip
        hh, mm = rng.randint(8, 19), rng.randint(0, 59)
        r = dict(key=key, merchant=merchant, template=template, date=rdate, lines=lines, domain=domain,
                 subtotal=subtotal, gst=gst, total=total, currency=currency, gst_number=gst_number,
                 refund=refund, address=extra.pop("address", city_addr()), time=f"{hh}:{mm:02d}",
                 invoice_no=extra.pop("invoice_no", f"{rng.randint(100000, 999999)}"),
                 dt=datetime(rdate.year, rdate.month, rdate.day, hh, mm) + timedelta(days=lag_email), **extra)
        if tip:
            r["tip_on_receipt"] = tip
        receipts[key] = r
        return r

    def txn(day, acct, desc, cents, gold, ledger=None, rkey=None, note=""):
        txns.append(dict(date=day, account=acct, description=desc, amount_cents=cents, gold_category=gold,
                         ledger_category=ledger or gold, receipt=rkey, note=note))

    def spend(day, acct, desc, gold, r=None, ledger=None, cents=None, note=""):
        amt = -(cents if cents is not None else r["total"])
        txn(day, acct, desc, amt, gold, ledger, r["key"] if r else None, note)

    # --- Meals 5030
    r = receipt("bluedoor-1", merchant="Blue Door Cafe", template="tillpoint", rdate=d(2), domain="bluedoorcafe.test",
                lines=[("1 x Oat latte", 525), ("1 x Butter croissant", 450)])
    spend(d(2), CARD, "SQ *BLUE DOOR CAFE TORONTO ON", "5030", r)
    r = receipt("bluedoor-2", merchant="Blue Door Cafe", template="tillpoint", rdate=d(8), domain="bluedoorcafe.test",
                lines=[("2 x Latte", 1050), ("2 x Blueberry muffin", 780)])
    spend(d(9), CARD, "SQ *BLUE DOOR CAFE TORONTO ON", "5030", r)
    r = receipt("bluedoor-3", merchant="Blue Door Cafe", template="tillpoint", rdate=d(15), domain="bluedoorcafe.test",
                lines=[("1 x Oat latte", 525), ("1 x Butter croissant", 450)])
    spend(d(16), CARD, "SQ *BLUE DOOR CAFE TORONTO ON", "5030", r)
    spend(d(27), CARD, "SQ *BLUE DOOR CAFE TORONTO ON", "5030", cents=1695)
    r = receipt("northbound-1", merchant="Northbound Coffee Roasters", template="tillpoint", rdate=d(6),
                domain="northbound.test", lines=[("2 x Cold brew", 1100)])
    spend(d(6), CARD, "TST* NORTHBOUND COFFEE #12", "5030", r)
    spend(d(20), CARD, "TST* NORTHBOUND COFFEE #12", "5030", cents=650)
    r = receipt("northbound-2", merchant="Northbound Coffee Roasters", template="tillpoint", rdate=d(28),
                domain="northbound.test", lines=[("1 x Espresso", 375), ("1 x Ham sandwich", 450)])
    spend(d(29), CARD, "TST* NORTHBOUND COFFEE #12", "5030", r)
    # Tip added at the table after the receipt was emailed: amount mismatch
    r = receipt("fennel-1", merchant="Fennel & Ash Bistro", template="plain", rdate=d(14), domain="fennelash.test",
                lines=[("Lunch - 2 covers", 6400), ("Sparkling water x2", 800), ("Dessert x2", 1200)])
    spend(d(15), CARD, "FENNEL & ASH BISTRO TORONT", "5030", r, cents=r["total"] + 1708, ledger="5070",
          note="tip added after receipt")
    # Receipt over $100 with HST but no GST/HST number printed
    r = receipt("fennel-2", merchant="Fennel & Ash Bistro", template="tillpoint", rdate=d(22), domain="fennelash.test",
                lines=[("Team dinner - 4 covers", 15600), ("Wine (bottle)", 3000)], tip=3782)
    spend(d(23), CARD, "SQ *FENNEL & ASH BISTRO", "5030", r)
    r = receipt("marigold-1", merchant="Marigold Bakery", template="tillpoint", rdate=d(9), domain="marigold.test",
                lines=[("Pastry box (12)", 1628)])
    spend(d(9), CARD, "SQ *MARIGOLD BAKERY", "5030", r, ledger="5040")
    r = receipt("saffron-1", merchant="Saffron Leaf Kitchen", template="saas", rdate=d(10), domain="dashly.test",
                lines=[("Delivery order: butter chicken, rice, naan (team lunch)", 3425)],
                invoice_no="DL-55821")
    spend(d(11), CARD, "DASHLY *SAFFRON LEAF", "5030", r, ledger="9999")
    r = receipt("greenbowl-1", merchant="Green Bowl Co", template="saas", rdate=d(23), domain="dashly.test",
                lines=[("Delivery order: 2 grain bowls (client working lunch)", 2429)], invoice_no="DL-57310")
    spend(d(23), CARD, "DASHLY *GREEN BOWL CO", "5030", r)
    r = receipt("rideau-1", merchant="Rideau Grill", template="plain", rdate=d(20), domain="rideaugrill.test",
                address="40 Elgin St, Ottawa, ON K1P 1C7", lines=[("Dinner - 2 covers", 6800)])
    spend(d(21), CARD, "RIDEAU GRILL OTTAWA ON", "5030", r)
    spend(d(20), CARD, "SQ *BYWARD BEANS OTTAWA", "5030", cents=840)

    # --- Office supplies 5040 / equipment 1500
    kgst = gst_no(rng)
    r = receipt("kestrel-1", merchant="Kestrel Marketplace", template="order", rdate=d(3), domain="kestrel.test",
                lines=[("Aluminium laptop stand", 4999), ("USB-C cable 2m (3-pack)", 3999)], gst_number=kgst,
                invoice_no="702-4418831-2209")
    spend(d(5), CARD, "KESTREL MKTP CA*2K4L19RT3", "5040", r, ledger="5050")
    r = receipt("kestrel-2", merchant="Kestrel Marketplace", template="order", rdate=d(12), domain="kestrel.test",
                lines=[("Copy paper, 5 reams", 3299)], gst_number=kgst, invoice_no="702-7731042-8812")
    spend(d(13), CARD, "KESTREL MKTP CA*7H2PQ04Z1", "5040", r)
    r = receipt("kestrel-3", merchant="Kestrel Marketplace", template="order", rdate=d(16), domain="kestrel.test",
                lines=[("Toner cartridge, black", 2899)], gst_number=kgst, invoice_no="702-1209554-3310")
    spend(d(17), CARD, "KESTREL MKTP CA*9W1MZ88K0", "5040", r)
    r = receipt("kestrel-refund", merchant="Kestrel Marketplace", template="refund", rdate=d(19), domain="kestrel.test",
                lines=[("Toner cartridge, black", 2899)], gst_number=kgst, invoice_no="702-1209554-3310", refund=True)
    txn(d(21), CARD, "KESTREL MKTP CA*RF8Q1 REFUND", r["total"], "5040", "4020", "kestrel-refund", "refund")
    spend(d(26), CARD, "KESTREL MKTP CA*3ZC6TT0Q8", "5040", cents=7344)
    r = receipt("paperline-1", merchant="Paperline Office Supply", template="plain", rdate=d(7), domain="paperline.test",
                lines=[("Notebooks A5 x6", 2394), ("Gel pens 12pk", 1695)], gst_number=gst_no(rng))
    spend(d(7), CARD, "PAPERLINE OFFICE #031 TORONTO", "5040", r)
    spend(d(29), CARD, "PAPERLINE OFFICE #031 TORONTO", "5040", cents=1240)
    r = receipt("deskcraft-1", merchant="Deskcraft Goods", template="order", rdate=d(24), domain="deskcraft.test",
                lines=[("Single monitor arm", 11900)], gst_number=gst_no(rng), invoice_no="DC-10442")
    spend(d(24), CARD, "SP * DESKCRAFT GOODS", "5040", r, ledger="9999")
    r = receipt("circuithub-1", merchant="Circuit Hub", template="pdf", rdate=d(10), domain="circuithub.test",
                lines=[('Laptop 14" 32GB / 1TB (serial CH-88213)', 189900)], gst_number=gst_no(rng),
                invoice_no="CH-118-00931", terms="Terms: paid in full by Visa")
    spend(d(10), CARD, "CIRCUIT HUB #118 TORONTO", "1500", r, ledger="5040")

    # --- Software 5050 (one USD charge)
    r = receipt("cloudnest-1", merchant="Cloudnest", template="saas", rdate=d(3), domain="cloudnest.test",
                lines=[("Cloudnest Team plan, 3 seats (monthly)", 4900)], currency="USD", invoice_no="CN-2026-10-0471")
    spend(d(5), CARD, "CLOUDNEST.IO SAN FRANCISCO CA", "5050", r, cents=6981, ledger="5040",
          note="USD 49.00 at 1.4247 incl. card FX fee")
    r = receipt("slatebox-1", merchant="Slatebox", template="saas", rdate=d(1), domain="slatebox.test",
                lines=[("Slatebox Pro subscription (monthly)", 2400)], gst_number=gst_no(rng), invoice_no="SB-88213")
    spend(d(1), CARD, "SLATEBOX* SUBSCR 8885550199 ON", "5050", r)
    r = receipt("tallyhub-1", merchant="Tallyhub", template="pdf", rdate=d(5), domain="tallyhub.test",
                lines=[("Tallyhub Essentials plan - October 2026", 4000)], gst_number=gst_no(rng), invoice_no="TH-3301927")
    spend(d(5), CARD, "TALLYHUB *ACCOUNTING", "5050", r)
    r = receipt("quillmail-1", merchant="Quillmail", template="saas", rdate=d(11), domain="quillmail.test",
                lines=[("Quillmail Starter, 2 mailboxes (monthly)", 1800)], gst_number=gst_no(rng), invoice_no="QM-447120")
    spend(d(12), CARD, "QUILLMAIL.TEST BILLING", "5050", r)
    spend(d(12), CARD, "QUILLMAIL.TEST BILLING", "5050", cents=r["total"], note="duplicate charge")
    r = receipt("inkframe-1", merchant="Inkframe Design Suite", template="saas", rdate=d(18), domain="inkframe.test",
                lines=[("Inkframe Design Suite single licence (monthly)", 5999)], gst_number=gst_no(rng),
                invoice_no="IF-2210934")
    spend(d(19), CARD, "INKFRAME*DESIGN 4165550123", "5050", r, ledger="9999")
    r = receipt("nimbus-1", merchant="Nimbus Drive", template="saas", rdate=d(4), domain="nimbusdrive.test",
                lines=[("Nimbus Drive 2 TB storage plan (monthly)", 1299)], invoice_no="ND-0098812")
    spend(d(4), CARD, "NIMBUS DRIVE STORAGE", "5050", r)

    # --- Telephone & internet 5060, utilities 5110, rent 5100, insurance 5120
    r = receipt("aurora-1", merchant="Aurora Mobile", template="pdf", rdate=d(9), domain="auroramobile.test",
                lines=[("Business plan, 1 line, Oct 9 - Nov 8", 6500)], gst_number=gst_no(rng), invoice_no="AM-61028377")
    spend(d(13), BANK, "PAD AURORA MOBILE", "5060", r)
    r = receipt("brookline-1", merchant="Brookline Fibre", template="pdf", rdate=d(12), domain="brookline.test",
                lines=[("Business fibre internet 1 Gbps - October", 8900)], gst_number=gst_no(rng),
                invoice_no="BF-2026-10-55102")
    spend(d(15), BANK, "BROOKLINE FIBRE PAD", "5060", r)
    r = receipt("lakeview-1", merchant="Lakeview Hydro", template="pdf", rdate=d(8), domain="lakeviewhydro.test",
                lines=[("Electricity - studio, Sep 5 to Oct 4", 6510), ("Delivery and regulatory charges", 1910)],
                gst_number=gst_no(rng), invoice_no="LH-7781203", terms="Terms: pre-authorized debit on Oct 16")
    spend(d(16), BANK, "LAKEVIEW HYDRO PAD", "5110", r, ledger="5060")
    r = receipt("kingsway-1", merchant="Kingsway Properties", template="pdf", rdate=d(1), domain="kingswayprop.test",
                lines=[("Studio rent, Unit 204 - October 2026", 240000)], gst_number=gst_no(rng),
                invoice_no="KP-2026-10-204", terms="Terms: pre-authorized debit on the 1st")
    spend(d(1), BANK, "PREAUTH DEBIT KINGSWAY PROP", "5100", r)
    r = receipt("ridgeway-1", merchant="Ridgeway Mutual Insurance", template="pdf", rdate=d(1), hst=False,
                domain="ridgewaymutual.test", lines=[("Commercial general liability - monthly premium", 11875)],
                invoice_no="RM-POL-4471-10")
    spend(d(2), BANK, "RIDGEWAY MUTUAL INS", "5120", r)

    # --- Travel 5070
    rider = fake.first_name()
    rides = [("zip-1", 6, 1630, "morning"), ("zip-2", 6, 1865, "evening"), ("zip-3", 14, 1405, "afternoon"),
             (None, 22, 2156, None), ("zip-5", 29, 1765, "morning")]
    for key, day, sub, when in rides:
        if key:
            fare = sub - 250
            r = receipt(key, merchant="ZipRide", template="ride", rdate=d(day), domain="zipride.test",
                        lines=[("Trip fare", fare), ("Booking fee", 250)], gst_number="78823 4410 RT0001",
                        rider=rider, when=when, pickup=fake.street_address(), dropoff=fake.street_address())
            spend(d(day + (1 if key == "zip-3" else 0)), CARD, "ZIPRIDE *TRIP HELP.ZIPRIDE", "5070", r,
                  ledger="5080" if key in ("zip-2", "zip-5") else None)
        else:
            spend(d(day), CARD, "ZIPRIDE *TRIP HELP.ZIPRIDE", "5070", cents=sub + gst_hst_cents(sub, "ON"))
    r = receipt("lakeshore-1", merchant="Lakeshore Rail", template="pdf", rdate=d(18), domain="lakeshorerail.test",
                lines=[("Train ticket Toronto - Ottawa return, 1 adult", 14200)], gst_number=gst_no(rng),
                invoice_no="LR-K7Q2M9", terms="Terms: paid by Visa")
    spend(d(19), CARD, "LAKESHORE RAIL 4021 TORONTO", "5070", r)
    r = receipt("wellesley-1", merchant="Wellesley Suites Hotel", template="folio", rdate=d(21), domain="wellesley.test",
                address="155 Wellington St, Ottawa, ON K1P 5B6", lines=[("Room charge 20/10", 18900)],
                other_tax=756, gst_number=gst_no(rng), invoice_no="F-300912", guest=owner)
    spend(d(21), CARD, "WELLESLEY SUITES OTTAWA ON", "5070", r)
    spend(d(19), CARD, "OTTAWA CITY TAXI 221", "5070", cents=3450)

    # --- Vehicle 5080
    r = receipt("northline-1", merchant="Northline Fuel", template="plain", rdate=d(3), domain="northlinefuel.test",
                address="2250 Dundas St W, Mississauga, ON L5K 1R5",
                lines=[("Regular unleaded 39.4 L", 5522)])
    spend(d(3), CARD, "NORTHLINE FUEL #4412 MISSISSAUGA", "5080", r)
    spend(d(17), CARD, "NORTHLINE FUEL #4412 MISSISSAUGA", "5080", cents=7115)
    spend(d(8), CARD, "CITYPARK LOT 214", "5080", cents=1200)
    spend(d(20), CARD, "CITYPARK LOT 214", "5080", cents=1800, ledger="9999")

    # --- Professional fees 5090 (split payment)
    cf, cl = contractor
    r = receipt("okafor-1", merchant=f"{cl} Studio", template="pdf", rdate=d(6), domain=f"{cl.lower()}studio.test",
                lines=[("Brand identity design services - phase 2", 250000)], gst_number=gst_no(rng),
                invoice_no="INV-0412", terms="Terms: 50% on receipt, balance net 21 days")
    sname = f"{cf[0]} {cl}".upper()
    txn(d(9), BANK, f"INTERAC E-TRF SENT {sname}", -150000, "5090", "3100", "okafor-1", "split 1/2")
    txn(d(9), BANK, "INTERAC E-TRF FEE", -150, "5020")
    txn(d(23), BANK, f"INTERAC E-TRF SENT {sname}", -132500, "5090", None, "okafor-1", "split 2/2")
    txn(d(23), BANK, "INTERAC E-TRF FEE", -150, "5020")
    r = receipt("halden-1", merchant="Halden & Co CPA", template="pdf", rdate=d(26), domain="haldencpa.test",
                lines=[("Accounting services - Q3 bookkeeping review", 95000)], gst_number=gst_no(rng),
                invoice_no="HC-2026-118", terms="Terms: net 15")
    spend(d(28), BANK, "BILL PMT HALDEN AND CO", "5090", r)

    # --- Shipping 5130, advertising 5010, training 5140
    r = receipt("parcelway-1", merchant="Parcelway", template="plain", rdate=d(7), domain="parcelway.test",
                lines=[("Expedited parcel to Halifax NS", 2310)])
    spend(d(8), CARD, "PARCELWAY 0012 TORONTO ON", "5130", r)
    r = receipt("parcelway-2", merchant="Parcelway", template="plain", rdate=d(24), domain="parcelway.test",
                lines=[("Priority parcel to Vancouver BC", 4180)])
    spend(d(24), CARD, "PARCELWAY 0012 TORONTO ON", "5130", r)
    spend(d(27), CARD, "PARCELWAY 0012 TORONTO ON", "5130", cents=1870)
    bgst = gst_no(rng)
    r = receipt("brightreach-1", merchant="BrightReach Ads", template="pdf", rdate=d(5), domain="brightreach.test",
                lines=[("Sponsored post campaign, Sep 28 - Oct 4", 15000)], gst_number=bgst, invoice_no="BR-4UX7-1001",
                terms="Terms: charged to card on file")
    spend(d(6), CARD, "BRIGHTREACH*ADS 4UX7", "5010", r, ledger="5050")
    r = receipt("brightreach-2", merchant="BrightReach Ads", template="pdf", rdate=d(19), domain="brightreach.test",
                lines=[("Sponsored post campaign, Oct 12 - Oct 18", 15000)], gst_number=bgst,
                invoice_no="BR-4UX7-1002", terms="Terms: charged to card on file")
    spend(d(20), CARD, "BRIGHTREACH*ADS 4UX7", "5010", r)
    r = receipt("harbour-1", merchant="Harbour Print Shop", template="tillpoint", rdate=d(12), domain="harbourprint.test",
                lines=[("Business cards, 500", 8500)])
    spend(d(12), CARD, "SQ *HARBOUR PRINT SHOP", "5010", r)
    r = receipt("learnwell-1", merchant="Learnwell", template="saas", rdate=d(13), domain="learnwell.test",
                lines=[("Course: Accessible web design certificate", 12900)], invoice_no="LW-90012")
    spend(d(13), CARD, "LEARNWELL.TEST COURSES", "5140", r, ledger="9999")

    # --- A few more ordinary lines
    r = receipt("bluedoor-4", merchant="Blue Door Cafe", template="tillpoint", rdate=d(22), domain="bluedoorcafe.test",
                lines=[("2 x Americano", 750), ("1 x Sesame bagel", 425)])
    spend(d(22), CARD, "SQ *BLUE DOOR CAFE TORONTO ON", "5030", r)
    spend(d(18), CARD, "SQ *MARIGOLD BAKERY", "5030", cents=980)
    r = receipt("northline-2", merchant="Northline Fuel", template="plain", rdate=d(24), domain="northlinefuel.test",
                address="2250 Dundas St W, Mississauga, ON L5K 1R5", lines=[("Regular unleaded 41.2 L", 6027)])
    spend(d(25), CARD, "NORTHLINE FUEL #4412 MISSISSAUGA", "5080", r)
    r = receipt("parcelway-3", merchant="Parcelway", template="plain", rdate=d(14), domain="parcelway.test",
                lines=[("Regular parcel to Montreal QC", 1650)])
    spend(d(14), CARD, "PARCELWAY 0012 TORONTO ON", "5130", r)
    r = receipt("harbour-2", merchant="Harbour Print Shop", template="tillpoint", rdate=d(30), domain="harbourprint.test",
                lines=[("Promotional flyers, 250", 6400)])
    spend(d(30), CARD, "SQ *HARBOUR PRINT SHOP", "5010", r, ledger="5040")
    txn(d(28), CARD, "ANNUAL FEE", -12000, "5020", "9999")
    # Harder cases: a French-language Quebec receipt, and a supplier whose card descriptor is
    # its numbered-company legal name rather than its trading name.
    sub = 6400
    r = receipt("plateau-1", merchant="Café du Plateau", template="fr", rdate=d(20, 10), domain="cafeduplateau.test",
                address="4120 rue Saint-Denis, Montréal, QC H2W 2M5", province="QC",
                lines=[("Dîner d'affaires - 2 couverts", sub)], gst_number=gst_no(rng),
                qst_number=f"{rng.randint(10**9, 10**10 - 1)}TQ0001", other_tax=int(sub * 0.09975 + 0.5))
    spend(d(22), CARD, "CAFE DU PLATEAU MONTREAL QC", "5030", r, ledger="5070")
    r = receipt("lumen-1", merchant="Lumen Lighting Studio", template="pdf", rdate=d(14), domain="lumenlighting.test",
                lines=[("LED desk lamps x2", 15800)], gst_number=gst_no(rng), invoice_no="LL-20417",
                terms="Terms: paid by Visa")
    spend(d(15), CARD, "2468013 ONTARIO INC", "5040", r, ledger="9999")
    r = receipt("slatebox-2", merchant="Slatebox", template="saas", rdate=d(15), domain="slatebox.test",
                lines=[("Slatebox Pro - 1 additional seat (prorated)", 1160)], gst_number=receipts["slatebox-1"]["gst_number"],
                invoice_no="SB-88940")
    spend(d(15), CARD, "SLATEBOX* SUBSCR 8885550199 ON", "5050", r)

    # --- Bank fees, income, transfers, remittance, draw (bank statement is the record)
    txn(d(30), BANK, "MONTHLY ACCOUNT FEE", -1995, "5020")
    txn(d(15), BANK, f"INTERAC E-TRF SENT {owner.upper()}", -200000, "3100", None, None, "owner draw")
    txn(d(15), BANK, "INTERAC E-TRF FEE", -150, "5020")
    for day, cents_ in [(2, 123456), (7, 98720), (13, 145010), (19, 87645), (26, 132299), (30, 76420)]:
        txn(d(day), BANK, "STRIPE TRANSFER ST-" + f"{rng.randint(10**7, 10**8 - 1)}", cents_, "4010",
            "5020" if day == 13 else None)
    txn(d(15), BANK, "EFT CREDIT MAPLEFORD ANALYTICS", 565000, "4020")
    txn(d(27), BANK, "MOBILE DEPOSIT", 226000, "4020", "9999")
    txn(d(25), BANK, "ONLINE BANKING PMT VISA 4421", -320000, "2100")
    txn(d(26), CARD, "PAYMENT - THANK YOU", 320000, "2100")
    txn(d(30), BANK, "GOVT CANADA GST34", -184622, "2200", "5020")

    # --- Receipts with no transaction (orphans) and one duplicate
    receipt("orphan-bluedoor", merchant="Blue Door Cafe", template="tillpoint", rdate=d(30), domain="bluedoorcafe.test",
            lines=[("1 x Chai latte", 575), ("1 x Scone", 425)])
    receipt("orphan-kestrel-sep", merchant="Kestrel Marketplace", template="order", rdate=d(28, 9),
            domain="kestrel.test", lines=[("Desk organiser tray", 5099)], gst_number=kgst, invoice_no="702-0045126-7781")
    receipt("orphan-estimate", merchant="Paperline Office Supply", template="pdf", rdate=d(29),
            domain="paperline.test", lines=[("Ergonomic chair (quote)", 21239)], doc_title="ESTIMATE",
            invoice_no="EST-2290", terms="Estimate valid 30 days. Not a request for payment.")
    receipt("orphan-spa", merchant="Glow Day Spa", template="tillpoint", rdate=d(17), domain="glowspa.test",
            lines=[("Massage 60 min", 9500)])
    receipts["brookline-dup"] = dict(receipts["brookline-1"], key="brookline-dup", duplicate_of="brookline-1",
                                     dt=receipts["brookline-1"]["dt"] + timedelta(days=4))
    return txns, receipts, owner


def main(out: Path = OUT) -> dict:
    random.seed(SEED)                       # email boundaries use the global RNG
    Faker.seed(SEED)
    fake = Faker("en_CA")
    rng = random.Random(SEED)
    txns, receipts, owner = build_month(fake, rng)

    out.mkdir(parents=True, exist_ok=True)
    (out / "chart_of_accounts.yaml").write_text(
        "# Synthetic chart of accounts for the bookkeeping module. Keywords drive the rules baseline only.\n"
        + yaml.safe_dump(dict(CHART, entity=dict(CHART["entity"], owner=owner)), sort_keys=False, width=110))

    txns.sort(key=lambda t: (t["date"], t["account"], t["description"], t["amount_cents"]))
    for i, t in enumerate(txns, 1):
        t["txn_id"] = f"T{i:03d}"
    with (out / "transactions.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "date", "account", "description", "amount"])
        for t in txns:
            w.writerow([t["txn_id"], t["date"].isoformat(), t["account"], t["description"],
                        f"{t['amount_cents'] / 100:.2f}"])
    with (out / "ledger_current.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "category"])
        for t in txns:
            w.writerow([t["txn_id"], t["ledger_category"]])

    box = out / "mailbox"
    box.mkdir(exist_ok=True)
    for old in box.glob("*.eml"):
        old.unlink()
    order = sorted(receipts.values(), key=lambda r: (r["dt"], r["key"]))
    rid_of = {}
    for i, r in enumerate(order, 1):
        rid = f"R{i:03d}"
        rid_of[r["key"]] = rid
        sender, subject, body, atts, dt = TEMPLATES[r["template"]](r, rng)
        if r.get("duplicate_of"):
            subject = f"Fwd: {subject}"
            body = (f"Forwarding for the books.\n\n---------- Forwarded message ---------\nFrom: {sender}\n"
                    f"Subject: {subject[5:]}\n\n{body}")
            sender = f"{owner} <owner@larchmont.test>"
        (box / f"{rid}.eml").write_bytes(build_eml(sender=sender, to=BOOKS, subject=subject, date=eml_date(dt),
                                                    body=body, attachments=atts))

    total_sign = lambda r: -r["total"] if r["refund"] else r["total"]  # noqa: E731
    labels = {
        "period": f"{YEAR}-{MONTH:02d}",
        "receipts": {
            rid_of[k]: {"key": k, "merchant": r["merchant"], "date": r["date"].isoformat(), "total_cents": total_sign(r),
                        "gst_hst_cents": -r["gst"] if r["refund"] else r["gst"], "currency": r["currency"],
                        "gst_number": (r["gst_number"] or "").replace(" ", "") or None,
                        "duplicate_of": rid_of.get(r.get("duplicate_of")),
                        "orphan": k.startswith("orphan")}
            for k, r in receipts.items()
        },
        "links": sorted([[rid_of[t["receipt"]], t["txn_id"]] for t in txns if t["receipt"]]),
        "categories": {t["txn_id"]: t["gold_category"] for t in txns},
        "ledger_wrong": sorted(t["txn_id"] for t in txns if t["ledger_category"] != t["gold_category"]),
        "notes": {t["txn_id"]: t["note"] for t in txns if t["note"]},
    }
    (out / "labels.json").write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    return {"transactions": len(txns), "receipts": len(receipts), "links": len(labels["links"])}


if __name__ == "__main__":
    print(main())
