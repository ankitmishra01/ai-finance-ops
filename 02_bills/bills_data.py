"""Seeded generator for the synthetic AP mailbox used by the bills module.

Writes `data/synthetic/bills/inbox/*.eml` plus `labels.json` (the hand-designed
ground truth for every message). Run it again and the bytes come out the same:

    uv run python 02_bills/bills_data.py

Every name, address and GST/HST number is made up. Email domains end in .test.
"""

from __future__ import annotations

import io
import json
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from faker import Faker  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from shared.mail import build_eml  # noqa: E402
from shared.tax import bn_check_digit  # noqa: E402

OUT = ROOT / "data" / "synthetic" / "bills"
AS_OF = date(2026, 11, 3)
RECIPIENT = "Tamarack Test Studio Inc."
RECIPIENT_ADDR = ["88 Spadina Ave, Unit 4", "Toronto, ON M5V 2J4"]
AP_MAILBOX = "ap@tamarack-studio.test"
EST = timezone(timedelta(hours=-5))

CITIES = {
    "ON": ("Ottawa", "K"), "NS": ("Halifax", "B"), "AB": ("Calgary", "T"), "BC": ("Victoria", "V"),
    "QC": ("Gatineau", "J"), "MB": ("Winnipeg", "R"),
}

fake = Faker("en_CA")
fake.seed_instance(42)
rng = random.Random(42)


def money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def bn_with_check_digit() -> str:
    """9-digit business number whose last digit is a Luhn check digit, plus RT0001."""
    base = "".join(map(str, [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(7)]))
    return base + str(bn_check_digit(base)) + "RT0001"


def vendor(sector: str, prov: str | None = "ON", *, display: str | None = None, country: str = "CA") -> dict:
    last = fake.last_name()
    name = f"{last} {sector}"
    slug = "".join(ch for ch in last.lower() if ch.isalpha())
    if country == "US":
        addr = [fake.street_address().replace("\n", " "), "Portland, OR 97204", "USA"]
    else:
        city, letter = CITIES[prov]
        pc = fake.postalcode_in_province(prov)
        addr = [fake.street_address().replace("\n", " "), f"{city}, {prov} {pc}"]
    return {
        "name": name, "addr": addr, "province": prov if country == "CA" else None, "country": country,
        "email": f"billing@{slug}-{sector.split()[0].lower()}.test", "display": display or name,
        "contact": fake.name(), "bn": bn_with_check_digit(),
    }


# --- PDF layouts -----------------------------------------------------------------------------

def _pdf(draw) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, invariant=1)  # invariant: byte-identical reruns
    draw(c)
    c.showPage()
    c.save()
    return buf.getvalue()


def fmt_amt(x: Decimal) -> str:
    return f"{x:,.2f}"


def pdf_layout_a(b: dict) -> bytes:
    """Classic: vendor block top left, label: value header fields, items table, totals."""
    v = b["vendor"]

    def draw(c):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, 740, v["name"])
        c.setFont("Helvetica", 10)
        for i, line in enumerate(v["addr"]):
            c.drawString(60, 724 - 13 * i, line)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(420, 740, "INVOICE")
        c.setFont("Helvetica", 10)
        y = 670
        for lab, val in [("Invoice #:", b["number"]), ("Invoice Date:", b["date"].isoformat()),
                         ("Terms:", b["terms"] or "")] + (
                [("GST/HST Reg. No.:", b["bn_shown"])] if b["bn_shown"] else []):
            if val:
                c.drawString(60, y, f"{lab} {val}")
                y -= 14
        c.drawString(330, 670, "Bill to:")
        c.drawString(330, 656, RECIPIENT)
        for i, line in enumerate(RECIPIENT_ADDR):
            c.drawString(330, 642 - 13 * i, line)
        y = 580
        c.setFont("Helvetica-Bold", 10)
        c.drawString(60, y, "Description")
        c.drawRightString(540, y, "Amount")
        c.setFont("Helvetica", 10)
        for desc, amt in b["items"]:
            y -= 16
            c.drawString(60, y, desc)
            c.drawRightString(540, y, fmt_amt(amt))
        y -= 30
        for lab, amt in b["totals"]:
            c.drawString(380, y, lab)
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
    return _pdf(draw)


def pdf_layout_b(b: dict) -> bytes:
    """Centred 'TAX INVOICE' title, long dates, vendor name and BN in the footer."""
    v = b["vendor"]

    def draw(c):
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(306, 750, "TAX INVOICE")
        c.setFont("Helvetica", 10)
        c.drawString(60, 710, f"Invoice Number {b['number']}")
        c.drawString(60, 696, f"Invoice Date {b['date'].strftime('%B %-d, %Y')}")
        if b["terms"]:
            c.drawString(60, 682, f"Payment Terms: {b['terms']}")
        c.drawString(330, 710, f"Bill to: {RECIPIENT}")
        c.drawString(330, 696, RECIPIENT_ADDR[1])
        y = 630
        for desc, amt in b["items"]:
            c.drawString(60, y, f"{desc}")
            c.drawRightString(540, y, f"${fmt_amt(amt)}")
            y -= 15
        y -= 20
        for lab, amt in b["totals"]:
            c.drawString(360, y, lab)
            c.drawRightString(540, y, f"${fmt_amt(amt)}")
            y -= 15
        c.setFont("Helvetica", 8)
        c.drawString(60, 90, v["name"])
        c.drawString(60, 79, " | ".join(v["addr"]))
        if b["bn_shown"]:
            c.drawString(60, 68, f"GST/HST # {b['bn_shown']}")
    return _pdf(draw)


def pdf_layout_c(b: dict) -> bytes:
    """Boxed info panel with 'Bill No.' / 'Issued' labels and day-first dates."""
    v = b["vendor"]

    def draw(c):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, 745, v["name"].upper())
        c.setFont("Helvetica", 9)
        c.drawString(60, 732, ", ".join(v["addr"]))
        c.rect(360, 650, 190, 80)
        c.setFont("Helvetica", 10)
        c.drawString(370, 715, f"Bill No. {b['number']}")
        c.drawString(370, 700, f"Issued {b['date'].strftime('%-d %b %Y')}")
        if b["terms"]:
            c.drawString(370, 685, f"Terms {b['terms']}")
        if b["bn_shown"]:
            c.drawString(370, 670, f"Business No. {b['bn_shown']}")
        c.drawString(60, 690, "Bill to:")
        c.drawString(60, 676, RECIPIENT)
        y = 600
        for desc, amt in b["items"]:
            c.drawString(60, y, desc)
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
        y -= 20
        for lab, amt in b["totals"]:
            c.drawString(380, y, lab)
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
        if b["currency"] == "USD":
            c.drawString(380, y - 5, "All amounts in USD")
    return _pdf(draw)


def pdf_statement(v: dict, lines: list[tuple[str, str, Decimal]], when: date) -> bytes:
    def draw(c):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, 740, v["name"])
        c.setFont("Helvetica", 10)
        c.drawString(60, 724, ", ".join(v["addr"]))
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, 690, "STATEMENT OF ACCOUNT")
        c.setFont("Helvetica", 10)
        c.drawString(60, 674, f"Statement date: {when.isoformat()}   Customer: {RECIPIENT}")
        y = 640
        for d, ref, amt in lines:
            c.drawString(60, y, f"{d}  {ref}")
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
        c.drawString(360, y - 15, "Balance outstanding")
        c.drawRightString(540, y - 15, fmt_amt(sum(a for _, _, a in lines)))
    return _pdf(draw)


def pdf_receipt(v: dict, ref: str, amt: Decimal, when: date) -> bytes:
    def draw(c):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(60, 740, v["name"])
        c.setFont("Helvetica-Bold", 14)
        c.drawString(60, 700, "PAYMENT RECEIPT")
        c.setFont("Helvetica", 10)
        c.drawString(60, 680, f"Received from: {RECIPIENT}")
        c.drawString(60, 666, f"Applied to invoice {ref}")
        c.drawString(60, 652, f"Date paid: {when.isoformat()}")
        c.drawString(60, 638, f"Amount paid: ${fmt_amt(amt)}  -  Thank you for your payment.")
    return _pdf(draw)


# --- scenarios ------------------------------------------------------------------------------

def make_bill(v, number, d, items, *, rate, terms, layout="a", currency="CAD", bn_shown="__vendor__",
              tax_label=None, pst_rate=Decimal("0"), total_override=None, category, flags=()) -> dict:
    sub = money(sum(a for _, a in items))
    tax = money(sub * Decimal(str(rate)))
    pst = money(sub * pst_rate)
    total = total_override if total_override is not None else sub + tax + pst
    bn = v["bn"] if bn_shown == "__vendor__" else bn_shown
    label = tax_label or (f"HST ({Decimal(str(rate)) * 100:.0f}%)" if rate > 0.05 else "GST (5%)")
    sub_label = {"a": "Subtotal", "b": "Amount before tax", "c": "Sub-total"}[layout]
    total_label = {"a": f"Total ({currency})", "b": "Amount due", "c": "Balance due"}[layout]
    totals = [(sub_label, sub)]
    if rate > 0:
        totals.append((label, tax))
    if pst > 0:
        totals.append((f"PST ({pst_rate * 100:.0f}%)", pst))
    totals.append((total_label, total))
    return {
        "vendor": v, "number": number, "date": d, "items": items, "terms": terms, "bn_shown": bn,
        "layout": layout, "currency": currency, "totals": totals,
        "label": {
            "vendor_name": v["name"], "gst_number": bn, "invoice_number": number,
            "invoice_date": d.isoformat(), "subtotal": str(sub), "tax": str(tax), "other_tax": str(pst),
            "total": str(total), "currency": currency, "terms": terms, "category": category,
            "vendor_province": v["province"], "vendor_country": v["country"],
        },
        "expected_flags": list(flags),
    }


def body_invoice_text(b: dict) -> str:
    """An invoice typed straight into the email body, as small vendors often do."""
    v, lab = b["vendor"], b["label"]
    lines = [f"Hi {RECIPIENT.split()[0]} team,", "", "Please see our invoice below.", "",
             f"Vendor: {v['name']}", *v["addr"], "",
             f"Invoice No: {b['number']}", f"Date: {b['date'].strftime('%b %-d, %Y')}"]
    if b["terms"]:
        lines.append(f"Terms: {b['terms']}")
    if b["bn_shown"]:
        lines.append(f"HST Registration: {b['bn_shown']}")
    lines.append(f"Bill to: {RECIPIENT}")
    lines.append("")
    for desc, amt in b["items"]:
        lines.append(f"  {desc} .......... ${fmt_amt(amt)}")
    lines.append("")
    for t, amt in b["totals"]:
        lines.append(f"{t}: ${fmt_amt(amt)}")
    lines += ["", "Thanks,", v["contact"], v["name"]]
    return "\n".join(lines)


def pdf_layout_d(v: dict, ref: str, when: date, rows, net, hst, total, terms_line) -> bytes:
    """Quote-style table with Qty/Item/Unit/Line total and 'Net amount' / 'Grand total' labels."""
    def draw(c):
        c.setFont("Helvetica-Bold", 15)
        c.drawString(60, 745, v["name"])
        c.setFont("Helvetica", 9)
        c.drawString(60, 732, " / ".join(v["addr"]))
        c.setFont("Helvetica", 10)
        c.drawString(60, 700, f"Invoice ref. {ref}")
        c.drawString(60, 686, f"Dated: {when.strftime('%-d %B %Y')}")
        c.drawString(60, 672, f"Customer: {RECIPIENT}")
        c.drawString(60, 658, f"HST no. {v['bn']}")
        y = 620
        for x, h in [(60, "Qty"), (100, "Item"), (380, "Unit"), (470, "Line total")]:
            c.drawString(x, y, h)
        for qty, item, unit, line in rows:
            y -= 15
            c.drawString(60, y, str(qty))
            c.drawString(100, y, item)
            c.drawRightString(420, y, fmt_amt(unit))
            c.drawRightString(540, y, fmt_amt(line))
        y -= 30
        for lab, amt in [("Net amount", net), ("HST 13% on net", hst), ("Grand total", total)]:
            c.drawString(360, y, lab)
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
        c.drawString(60, y - 20, terms_line)
    return _pdf(draw)


def pdf_french(v: dict, ref: str, when: date, desc: str, sub, gst, qst, total) -> bytes:
    """A Quebec supplier's invoice with French labels (TPS = GST, TVQ = QST)."""
    def draw(c):
        c.setFont("Helvetica-Bold", 15)
        c.drawString(60, 745, v["name"])
        c.setFont("Helvetica", 9)
        c.drawString(60, 732, ", ".join(v["addr"]))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(430, 745, "FACTURE")
        c.setFont("Helvetica", 10)
        y = 700
        for line in [f"Facture no : {ref}", f"Date : {when.isoformat()}", "Conditions : Net 30",
                     f"No TPS : {v['bn']}", "No TVQ : 1006543210TQ0001", f"Client : {RECIPIENT}"]:
            c.drawString(60, y, line)
            y -= 14
        c.drawString(60, 600, desc)
        c.drawRightString(540, 600, fmt_amt(sub))
        y = 560
        for lab, amt in [("Sous-total", sub), ("TPS (5 %)", gst), ("TVQ (9,975 %)", qst), ("Total", total)]:
            c.drawString(380, y, lab)
            c.drawRightString(540, y, fmt_amt(amt))
            y -= 15
    return _pdf(draw)


def add_holdout(msgs: list[dict], add_other) -> None:
    """Six messages in formats the rules baseline was not written for."""
    D = lambda m, d: date(2026, m, d)  # noqa: E731

    def bill_label(v, number, d, sub, tax, total, terms, category, other="0.00", flags=()):
        return {"doc_type": "bill", "split": "holdout", "vendor_name": v["name"], "gst_number": v["bn"],
                "invoice_number": number, "invoice_date": d.isoformat(), "subtotal": sub, "tax": tax,
                "other_tax": other, "total": total, "currency": "CAD", "terms": terms, "category": category,
                "vendor_province": v["province"], "vendor_country": "CA", "expected_flags": list(flags)}

    # H1: invoice written as prose, vendor only in the signature
    bk = vendor("Bookkeeping Services", "ON")
    msgs.append({"sender": f"{bk['contact']} <{bk['email']}>", "subject": "October bookkeeping",
                 "date": msg_date(D(11, 2)), "attachments": [],
                 "body": f"Hi there,\n\nFor the October bookkeeping work, our invoice #INV-88 dated November 2, 2026 "
                         f"comes to $640.00 plus HST of $83.20, for a total of $723.20. Payment is due within "
                         f"30 days.\nOur HST number is {bk['bn'][:9]} {bk['bn'][9:]}.\n\nBest,\n{bk['contact']}\n"
                         f"{bk['name']}\n{', '.join(bk['addr'])}\n",
                 "label": bill_label(bk, "INV-88", D(11, 2), "640.00", "83.20", "723.20", "Net 30",
                                     "professional_fees")})
    # H2: quote-style PDF table
    fd = vendor("Furniture Depot Ltd.", "ON")
    net, hst = Decimal("1250.00"), Decimal("162.50")
    msgs.append({"sender": f"{fd['name']} <{fd['email']}>", "subject": "Your furniture order",
                 "date": msg_date(D(11, 2)), "body": "Hello, your invoice is attached. Thank you for your order.\n",
                 "attachments": [("FD-7781.pdf", "application/pdf", pdf_layout_d(
                     fd, "FD-7781", D(11, 2), [(2, "Standing desk", Decimal("625.00"), net)], net, hst,
                     net + hst, "Payment due within 14 days of the invoice date."))],
                 "label": bill_label(fd, "FD-7781", D(11, 2), "1250.00", "162.50", "1412.50", "Net 14", "office")})
    # H3: Quebec supplier, French labels, GST + QST
    qc = vendor("Traduction Inc.", "QC")
    sub, gst, qst = Decimal("500.00"), Decimal("25.00"), Decimal("49.88")
    msgs.append({"sender": f"{qc['name']} <{qc['email']}>", "subject": f"Facture QC-4410 - {qc['name']}",
                 "date": msg_date(D(10, 19)), "body": "Bonjour,\n\nVeuillez trouver ci-joint notre facture.\n",
                 "attachments": [("facture-QC-4410.pdf", "application/pdf", pdf_french(
                     qc, "QC-4410", D(10, 19), "Traduction de documents, 4 000 mots", sub, gst, qst, sub + gst + qst))],
                 "label": bill_label(qc, "QC-4410", D(10, 19), "500.00", "25.00", "574.88", "Net 30",
                                     "professional_fees", other="49.88")})
    # H4: a card-charge confirmation (receipt) that mentions an invoice and a total
    add_other("receipt", "Billing <billing@notely-app.test>", "Your subscription renewal",
              "Your card ending 4242 was charged $45.20 for your Pro subscription renewal.\n"
              "Invoice INV-2231\nTotal charged: $45.20\nNo action is needed.\n", D(10, 24))
    # H5: an account summary (statement) without the word "statement"
    add_other("statement", f"{fd['name']} <{fd['email']}>", "Account summary - October",
              "Account summary for your business account\n\nInvoice FD-7702 (Oct 3): $300.00\n"
              "Invoice FD-7781 (Nov 2): $1,412.50\nTotal outstanding: $1,712.50\n", D(11, 2))
    # H6: a colleague forwards a mover's invoice
    mv = vendor("Moving Co.", "ON")
    msgs.append({"sender": "Kai Morgan <kai@tamarack-studio.test>", "subject": "Fwd: invoice from the movers",
                 "date": msg_date(D(10, 31)), "attachments": [],
                 "body": "Can you pay this one? Thanks, Kai\n\n---------- Forwarded message ---------\n"
                         f"From: {mv['name']} <{mv['email']}>\nSubject: Invoice MV-2210\n\n"
                         f"Invoice No: MV-2210\nDate: Oct 30, 2026\nTerms: Net 10\nGST/HST No: {mv['bn']}\n\n"
                         "Office move, 2 movers, 4 hours ... $520.00\n\nSubtotal: $520.00\nHST: $67.60\n"
                         "Total: $587.60\n",
                 "label": bill_label(mv, "MV-2210", D(10, 30), "520.00", "67.60", "587.60", "Net 10", "general")})


def msg_date(d: date, hour: int = 9) -> str:
    return format_datetime(datetime.combine(d, time(hour, rng.randint(0, 59)), EST))


def build() -> tuple[list[dict], list[dict]]:
    """Return (messages, labels). Each message: filename, sender, subject, date, body, attachments."""
    D = lambda m, d: date(2026, m, d)  # noqa: E731
    msgs: list[dict] = []
    # "templated" messages use the layouts the rules baseline was written against; "holdout"
    # messages use wording and layouts it was not tuned on, so the eval can show the gap.
    state = {"split": "templated"}

    def add_bill(b, *, subject, body=None, in_body=False, sent=None, display=None):
        v = b["vendor"]
        sender = f"{display or v['display']} <{v['email']}>"
        atts = []
        if in_body:
            text = body_invoice_text(b)
        else:
            text = body or f"Hello,\n\nPlease find invoice {b['number']} attached.\n\nRegards,\n{v['contact']}"
            pdf = {"a": pdf_layout_a, "b": pdf_layout_b, "c": pdf_layout_c}[b["layout"]](b)
            atts = [(f"invoice-{b['number']}.pdf", "application/pdf", pdf)]
        msgs.append({"sender": sender, "subject": subject, "date": msg_date(sent or b["date"]),
                     "body": text, "attachments": atts,
                     "label": {"doc_type": "bill", "split": state["split"], **b["label"],
                               "expected_flags": b["expected_flags"]}})

    def add_other(kind, sender, subject, body, d, atts=()):
        msgs.append({"sender": sender, "subject": subject, "date": msg_date(d), "body": body,
                     "attachments": list(atts), "label": {"doc_type": kind, "split": state["split"], "expected_flags": []}})

    # 1 SaaS, ON, layout A, Net 30
    saas = vendor("Cloud Software Inc.", "ON")
    b1 = make_bill(saas, "CS-20481", D(10, 15),
                   [("Team plan, 12 seats, Oct 2026", Decimal("540.00")), ("Extra storage 200 GB", Decimal("36.00"))],
                   rate=0.13, terms="Net 30", category="software")
    add_bill(b1, subject=f"Invoice CS-20481 from {saas['name']}")
    # 2 Office supplies under $100, ON, layout B
    office = vendor("Office Supply Ltd.", "ON")
    add_bill(make_bill(office, "OS-7712", D(10, 28),
                       [("Printer paper, 5 reams", Decimal("42.50")), ("Toner cartridge", Decimal("32.50"))],
                       rate=0.13, terms="Net 15", layout="b", category="office"),
             subject="Your order invoice OS-7712")
    # 3 Accountant, ON, layout C, over $500
    acct = vendor("Accounting LLP", "ON", display="Accounts Receivable")
    add_bill(make_bill(acct, "2026-0193", D(10, 20),
                       [("Monthly bookkeeping review, September", Decimal("1600.00")),
                        ("Year-end planning meeting", Decimal("800.00"))],
                       rate=0.13, terms="Net 15", layout="c", category="professional_fees"),
             subject="Invoice 2026-0193")
    # 4 Rent, due on receipt, ON, overdue
    landlord = vendor("Properties Ltd.", "ON")
    add_bill(make_bill(landlord, "R-1101", D(10, 1), [("Rent, Unit 4, November 2026", Decimal("3500.00"))],
                       rate=0.13, terms="Due on receipt", category="rent"),
             subject="November rent invoice R-1101")
    # 5 Alberta vendor charging 5% GST (consistent with its address)
    ab = vendor("Data Hosting Corp.", "AB")
    add_bill(make_bill(ab, "DH-5590", D(10, 25), [("Managed hosting, October", Decimal("220.00"))],
                       rate=0.05, terms="Net 30", layout="b", category="software"),
             subject="DH-5590 invoice")
    # 6 BC telecom: GST 5% + PST 7%
    bc = vendor("Telecom Ltd.", "BC")
    add_bill(make_bill(bc, "TL-88213", D(10, 18), [("Business phone lines, October", Decimal("180.00"))],
                       rate=0.05, pst_rate=Decimal("0.07"), terms="Net 30", layout="a", category="utilities"),
             subject="Your Telecom bill TL-88213")
    # 7 Courier, body-pasted, under $100
    courier = vendor("Courier Services", "ON")
    add_bill(make_bill(courier, "CR-3302", D(10, 30), [("Same-day delivery x3", Decimal("55.00"))],
                       rate=0.13, terms="Net 7", category="shipping"),
             subject="Invoice CR-3302", in_body=True)
    # 8 Marketing agency, body-pasted, Net 60
    agency = vendor("Creative Agency", "ON")
    add_bill(make_bill(agency, "CA-0457", D(10, 22),
                       [("Social media campaign, October", Decimal("1200.00"))],
                       rate=0.13, terms="Net 60", category="advertising"),
             subject="October campaign invoice", in_body=True)
    # 9 Duplicate resend of 1
    add_bill(make_bill(saas, "CS-20481", D(10, 15),
                       [("Team plan, 12 seats, Oct 2026", Decimal("540.00")), ("Extra storage 200 GB", Decimal("36.00"))],
                       rate=0.13, terms="Net 30", category="software", flags=["duplicate"]),
             subject=f"RE: Invoice CS-20481 from {saas['name']} (resend)",
             body="Hello,\n\nResending the invoice below in case it was missed.\n\nRegards,\nBilling",
             sent=D(11, 2))
    # 10 NS vendor charging 15% HST (NS is 14%; recipient in ON is 13%)
    ns = vendor("Marine Consulting", "NS")
    add_bill(make_bill(ns, "MC-118", D(10, 12), [("Advisory services, October", Decimal("900.00"))],
                       rate=0.15, terms="Net 30", layout="c", category="professional_fees", flags=["tax_rate"]),
             subject="Invoice MC-118")
    # 11 Contractor with no GST/HST number, $450 + HST
    handy = vendor("Renovations", "ON")
    add_bill(make_bill(handy, "1045", D(10, 26), [("Repair office door and frame", Decimal("450.00"))],
                       rate=0.13, terms="Net 15", bn_shown=None, category="repairs", flags=["missing_bn"]),
             subject="Invoice for door repair", in_body=True)
    # 12 US vendor, no GST, USD
    us = vendor("Analytics LLC", None, country="US")
    add_bill(make_bill(us, "US-2026-1031", D(10, 31), [("Analytics Pro plan, annual", Decimal("299.00"))],
                       rate=0, terms="Due on receipt", layout="c", currency="USD", bn_shown=None,
                       category="software", flags=["foreign_currency"]),
             subject=f"Invoice US-2026-1031 from {us['name']}")
    # 13 Total mismatch: printed total is $10 high
    print_shop = vendor("Print Shop", "ON")
    pb = make_bill(print_shop, "PS-6620", D(10, 21), [("Brochures, 500 copies", Decimal("350.00"))],
                   rate=0.13, terms="Net 30", layout="b", category="advertising", flags=["total_mismatch"],
                   total_override=Decimal("405.50"))
    add_bill(pb, subject="Print job invoice PS-6620")
    # 14 Catering (meals, 50% ITC)
    cater = vendor("Catering Co.", "ON")
    add_bill(make_bill(cater, "CT-940", D(10, 29), [("Client lunch catering, 12 guests", Decimal("380.00"))],
                       rate=0.13, terms="Net 15", category="meals"),
             subject="Catering invoice CT-940")
    # 15 & 16 Cleaning service: second bill looks like a re-issue under a new number
    clean = vendor("Cleaning Services", "ON")
    add_bill(make_bill(clean, "CL-1043", D(10, 31), [("Office cleaning, October", Decimal("260.00"))],
                       rate=0.13, terms="Net 30", layout="a", category="repairs"),
             subject="Invoice CL-1043")
    add_bill(make_bill(clean, "CL-1044", D(11, 1), [("Office cleaning, October", Decimal("260.00"))],
                       rate=0.13, terms="Net 30", layout="a", category="repairs", flags=["possible_duplicate"]),
             subject="Invoice CL-1044")
    # 17 Small cafe bill under $100 with no BN: allowed for ITC at this tier
    cafe = vendor("Coffee Bar", "ON")
    add_bill(make_bill(cafe, "CB-77", D(10, 30), [("Coffee and pastries for team meeting", Decimal("48.00"))],
                       rate=0.13, terms="Due on receipt", bn_shown=None, category="meals"),
             subject="Invoice CB-77", in_body=True)
    # 18 Early-payment discount terms
    supplies = vendor("Industrial Supply", "MB")
    add_bill(make_bill(supplies, "IS-3020", D(10, 27), [("Shelving units x4", Decimal("760.00"))],
                       rate=0.05, terms="2/10 Net 30", layout="b", category="office"),
             subject="Invoice IS-3020", display="Billing Team")
    # 19 Malformed registration number
    sign = vendor("Sign Makers", "ON")
    add_bill(make_bill(sign, "SM-301", D(10, 10), [("Window decal", Decimal("150.00"))],
                       rate=0.13, terms="Net 30", layout="c", bn_shown="87654321RT001",
                       category="advertising", flags=["invalid_bn"]),
             subject="Invoice SM-301")

    # --- not bills ---
    add_other("statement", f"{saas['name']} <{saas['email']}>", "Statement of account - October 2026",
              "Hello,\n\nYour October statement is attached. No action needed if already paid.\n",
              D(11, 1), [("statement-oct.pdf", "application/pdf",
                          pdf_statement(saas, [("2026-09-15", "Invoice CS-19870", Decimal("650.88")),
                                               ("2026-10-15", "Invoice CS-20481", Decimal("650.88"))], D(10, 31)))])
    add_other("statement", f"Accounts Receivable <{acct['email']}>", "Monthly account statement",
              f"Account statement for {RECIPIENT} as of October 31, 2026\n\n"
              "Opening balance: $0.00\nInvoice 2026-0193: $2,712.00\nPayments: $0.00\n"
              "Balance outstanding: $2,712.00\n\nThis is a statement, not an invoice.\n", D(11, 1))
    add_other("statement", f"{landlord['name']} <{landlord['email']}>", "Tenant ledger statement",
              "Hi,\n\nYour tenant statement for 2026 is below.\n\nJan-Oct rent: paid\nNovember rent R-1101: $3,955.00 open\n"
              "Total balance: $3,955.00\n", D(11, 2))
    add_other("receipt", f"{office['name']} <{office['email']}>", "Payment received - thank you",
              "We have received your payment of $96.05 for invoice OS-7610. Thank you for your payment.\n", D(10, 20))
    add_other("receipt", f"{courier['name']} <{courier['email']}>", "Receipt for your payment",
              "Thanks! This is your receipt.\n", D(10, 25),
              [("receipt-CR-3201.pdf", "application/pdf", pdf_receipt(courier, "CR-3201", Decimal("62.15"), D(10, 25)))])
    add_other("receipt", f"Billing <{agency['email']}>", "Paid: invoice CA-0401",
              "Invoice CA-0401 has been paid in full. Amount paid: $1,356.00. Total: $1,356.00.\n"
              "No further action is required.\n", D(10, 5))
    add_other("other", "Webinars <events@ledgerlearn.test>", "Free webinar: faster invoicing for small teams",
              "Join our free webinar on invoicing best practices. Plans from $29/month.\n", D(10, 28))
    add_other("other", "Deals <promo@deskmart.test>", "Your exclusive offer: save on office chairs",
              "This week only: ergonomic chairs. Total savings up to $120.00 when you order before Friday.\n"
              "Unsubscribe any time.\n", D(10, 29))
    add_other("other", f"Kai Morgan <kai@{AP_MAILBOX.split('@')[1]}>", "Lunch on Thursday?",
              "Hey, are we still on for lunch Thursday? I can book a table for 4.\n", D(11, 2))
    add_other("other", "Newsletter <news@smallbizdigest.test>", "Small business tax tips for November",
              "Five tips for the GST/HST filing season. Read more on our blog.\n", D(11, 2))
    add_other("other", f"{saas['name']} <noreply@{saas['email'].split('@')[1]}>",
              "Your invoice is available in the portal",
              "Your latest invoice for the Team plan is ready to view. Log in to the billing portal to "
              "download it. Do not reply to this email.\n", D(11, 1))

    state["split"] = "holdout"
    add_holdout(msgs, add_other)

    # Interleave chronologically by message date (stable), then number the files.
    for i, m in enumerate(msgs):
        m["_order"] = i
    msgs.sort(key=lambda m: (datetime.strptime(m["date"][5:25], "%d %b %Y %H:%M:%S"), m["_order"]))
    labels = []
    for i, m in enumerate(msgs, 1):
        m["filename"] = f"{i:02d}.eml"
        labels.append({"file": m["filename"], **m["label"]})
    return msgs, labels


def main() -> None:
    msgs, labels = build()
    inbox = OUT / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for old in inbox.glob("*.eml"):
        old.unlink()
    for m in msgs:
        # Pin the MIME boundary so reruns are byte-identical.
        raw = build_eml(sender=m["sender"], to=AP_MAILBOX, subject=m["subject"], date=m["date"],
                        body=m["body"], attachments=m["attachments"],
                        boundary=f"==============={m['filename'][:2]}{'0' * 17}==")
        (inbox / m["filename"]).write_bytes(raw)
    meta = {"as_of": AS_OF.isoformat(), "recipient": RECIPIENT, "recipient_province": "ON",
            "generator": "02_bills/bills_data.py (Faker en_CA, seed 42)", "items": labels}
    (OUT / "labels.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {len(msgs)} messages to {inbox}")


if __name__ == "__main__":
    main()
