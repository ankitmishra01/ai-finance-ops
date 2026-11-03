"""Seeded generator for the invoicing module's synthetic data.

Writes to data/synthetic/invoicing/:
  ops.json            mock ERP export: supplier, clients, customer records, contracts,
                      timesheets, milestones and invoices already issued
  mailbox/*.eml       the month's client email threads (billing details and changes)
  eval/mail/*.eml     extra onboarding threads used only by the eval
  eval/cases.json     one entry per labelled extraction case (contract text + emails)
  eval/labels.json    hand-specified ground truth per case, written by this script

Run:  uv run python 01_invoicing/invoicing_data.py
Everything is fictional. Email domains end in .test.
"""

from __future__ import annotations

import json
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from faker import Faker  # noqa: E402

from shared.mail import build_eml  # noqa: E402

OUT = ROOT / "data" / "synthetic" / "invoicing"
SEED = 42
PERIOD = "2026-10"
SUPPLIER_DOMAIN = "tamaracklane.test"
TZ = timezone(timedelta(hours=-4))

PROVINCE_NAMES = {
    "ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta",
    "MB": "Manitoba", "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "PE": "Prince Edward Island",
}
CITIES = {
    "ON": ["Toronto", "Ottawa", "Hamilton", "London", "Kitchener"],
    "QC": ["Montreal", "Quebec City", "Laval", "Gatineau"],
    "BC": ["Vancouver", "Victoria", "Kelowna", "Burnaby"],
    "AB": ["Calgary", "Edmonton", "Red Deer"],
    "MB": ["Winnipeg", "Brandon"],
    "SK": ["Saskatoon", "Regina"],
    "NS": ["Halifax", "Dartmouth"],
    "NB": ["Moncton", "Fredericton", "Saint John"],
    "NL": ["St. John's", "Corner Brook"],
    "PE": ["Charlottetown", "Summerside"],
}
SECTORS = ["Logistics", "Foods", "Health", "Systems", "Robotics", "Outfitters", "Analytics",
           "Builders", "Media", "Energy", "Dental Group", "Labs", "Freight", "Clinics", "Software"]
SUFFIXES = ["Inc.", "Ltd.", "Corp.", "Limited"]
STAFF = [("Senior consultant", 22500), ("Consultant", 16500), ("Analyst", 12000)]


class Gen:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        Faker.seed(SEED)
        self.fake = Faker("en_CA")
        self.used_names: set[str] = set()

    # -- small builders -------------------------------------------------
    def company(self) -> tuple[str, str, str]:
        """Return (legal_name, short name, domain)."""
        while True:
            last = self.fake.last_name()
            sector = self.rng.choice(SECTORS)
            legal = f"{last} {sector} {self.rng.choice(SUFFIXES)}"
            if legal not in self.used_names:
                self.used_names.add(legal)
                short = f"{last} {sector}"
                domain = re.sub(r"[^a-z]", "", (last + sector).lower()) + ".test"
                return legal, short, domain

    def address(self, prov: str) -> dict:
        street = self.fake.street_address()
        city = self.rng.choice(CITIES[prov])
        postal = self.fake.postalcode_in_province(prov).replace(" ", "")
        postal = f"{postal[:3]} {postal[3:]}"
        return {"street": street, "city": city, "province": prov, "postal": postal,
                "line": f"{street}, {city}, {prov} {postal}"}

    def person(self, domain: str) -> dict:
        first, last = self.fake.first_name(), self.fake.last_name()
        handle = re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())
        return {"name": f"{first} {last}", "email": f"{handle}@{domain}",
                "title": self.rng.choice(["Controller", "Director of Finance", "COO",
                                          "Operations Manager", "VP Finance", "Office Manager"])}

    def po(self) -> str:
        style = self.rng.randrange(3)
        if style == 0:
            return f"45000{self.rng.randrange(10000, 99999)}"
        if style == 1:
            return f"PO-2026-{self.rng.randrange(1000, 9999)}"
        return f"{self.rng.randrange(100000, 999999)}"

    def when(self, day: int, hour: int = 9) -> str:
        return format_datetime(datetime(2026, 10, day, hour, self.rng.randrange(60), tzinfo=TZ))


def contract_text(supplier: str, legal: str, addr: dict | None, kind: str, rate_txt: str,
                  terms: str, trade_name: str | None = None, prov_full: bool = False) -> str:
    who = f'{trade_name} ("Client")' if trade_name else f'{legal} ("Client")'
    if addr:
        prov = PROVINCE_NAMES[addr["province"]] if prov_full else addr["province"]
        where = f", having its office at {addr['street']}, {addr['city']}, {prov} {addr['postal']}"
    else:
        where = ""
    return (
        f"ENGAGEMENT LETTER\n\nThis engagement letter is made between {supplier} (\"Consultant\") "
        f"and {who}{where}.\n\n"
        f"1. Services. Consultant will provide {kind} advisory services as described in Schedule A.\n"
        f"2. Fees. {rate_txt} All amounts are in Canadian dollars, plus applicable GST/HST.\n"
        f"3. Payment terms. Invoices are payable {terms}.\n"
        f"4. Term. This letter takes effect on 1 October 2026 and continues until terminated "
        f"by either party on 30 days' written notice.\n"
    )


def onboarding_case(g: Gen, supplier: dict, variant: int, day: int) -> dict:
    """One new-client onboarding: contract text + 1-2 emails + ground-truth label."""
    legal, short, domain = g.company()
    prov = g.rng.choice(list(CITIES))
    addr = g.address(prov)
    contact = g.person(domain)
    sup_contact = supplier["contact_name"]
    po = None
    billing = None
    label_addr = addr
    label_legal = legal
    trade = None
    prov_full = False
    emails: list[dict] = []

    def mail(body: str, subject: str, d: int, sender: dict = contact) -> dict:
        return {"sender": f'"{sender["name"]}" <{sender["email"]}>', "to": supplier["email"],
                "subject": subject, "date": g.when(d), "body": body}

    sig = f"\n\n{contact['name']}\n{contact['title']}, {short}\n{contact['email']}\n"

    if variant == 0:
        billing = f"ap@{domain}"
        po = g.po()
        emails.append(mail(
            f"Hi {sup_contact},\n\nThanks for sending over the engagement letter, it's signed and attached. "
            f"For billing, please send invoices to {billing}. Our PO number for this engagement is {po}."
            f"\n\nBest,{sig}", "Signed engagement letter", day))
    elif variant == 1:
        billing = f"accounts.payable@{domain}"
        emails.append(mail(
            f"Hello {sup_contact},\n\nGreat to be working together. Our accounts payable team "
            f"({billing}) processes all vendor invoices, so please copy them on anything billing related. "
            f"We don't use purchase orders for professional services.\n\nThanks,{sig}",
            "Re: Welcome aboard", day))
    elif variant == 2:
        billing = f"invoices@{domain}"
        po = g.po()
        new_prov = g.rng.choice([p for p in CITIES if p != prov])
        label_addr = g.address(new_prov)
        emails.append(mail(
            f"Hi {sup_contact},\n\nQuick admin note before your first invoice. Please quote purchase order "
            f"P.O. #{po} and address the invoice to our {label_addr['city']} office rather than head office:\n\n"
            f"{label_addr['street']}\n{label_addr['city']}, {label_addr['province']} {label_addr['postal']}\n\n"
            f"Invoices go to {billing}.\n\nRegards,{sig}", "Billing details", day))
    elif variant == 3:
        billing = contact["email"]
        emails.append(mail(
            f"Hi {sup_contact},\n\nWe're a small team, so there's no AP department. You can invoice me "
            f"directly at this address. No PO needed.\n\nCheers,{sig}", "Invoicing", day))
    elif variant == 4:
        billing = None
        po = g.po()
        emails.append(mail(
            f"Hi {sup_contact},\n\nAttached is the countersigned letter. Our finance team has opened "
            f"purchase order {po} for this work. I'll confirm our AP contact next week once our new "
            f"controller starts.\n\nBest,{sig}", "Countersigned letter", day))
    elif variant == 5:
        billing = f"billing@{domain}"
        trade = short
        label_legal = legal
        emails.append(mail(
            f"Hi {sup_contact},\n\nOne thing for your billing team: the letter uses our operating name, "
            f"but invoices must show our legal name, {legal}, or our system will reject them. "
            f"Please email invoices to {billing}.\n\nThank you,{sig}", "Name on invoices", day))
    elif variant == 6:
        old = f"ap@{domain}"
        billing = f"payables@{domain}"
        po = g.po()
        first = (f"Hi {sup_contact},\n\nPlease send invoices to {old} and reference PO {po}.\n\nThanks,{sig}")
        emails.append(mail(first, "Billing contact", day))
        quoted = "\n".join("> " + ln for ln in first.splitlines())
        emails.append(mail(
            f"Hi again,\n\nCorrection to my note below: our AP mailbox has moved and the old one is no "
            f"longer monitored. The right address is now {billing}. The PO is unchanged.\n\nSorry for the "
            f"confusion,{sig}\nOn {g.when(day, 9)}, {contact['name']} wrote:\n{quoted}\n",
            "Re: Billing contact", day + 1))
    elif variant == 7:  # province spelled out, PO in words
        billing = f"ap@{domain}"
        po = g.po()
        prov_full = True
        emails.append(mail(
            f"Hello {sup_contact},\n\nPlease direct all invoices to {billing}. Our purchase order number "
            f"is {po}; it needs to appear on every invoice.\n\nKind regards,{sig}", "AP setup", day))
    # Variants 8-11 are held-out phrasings, written after the rules baseline was built and not
    # used to tune it. They appear only in the eval set.
    elif variant == 8:  # "bills" not "invoices"; no billing keyword in the sentence
        billing = f"remittance@{domain}"
        emails.append(mail(
            f"Hi {sup_contact},\n\nKindly route your bills to {billing} so they land with the right "
            f"people. No PO on our side.\n\nThanks,{sig}", "Paperwork", day))
    elif variant == 9:  # legal name given as "registered as"
        billing = f"ap@{domain}"
        trade = short
        emails.append(mail(
            f"Hi {sup_contact},\n\nOn paper we're registered as {legal}, so that's what should go on "
            f"the invoice. Send invoices to {billing}.\n\nBest,{sig}", "Invoice name", day))
    elif variant == 10:  # unit prefix on the bill-to address, one line
        billing = f"ap@{domain}"
        unit = f"Unit {g.rng.randrange(2, 40)}"
        label_addr = dict(addr)
        label_addr["street"] = f"{unit}, {addr['street']}"
        label_addr["line"] = f"{unit}, {addr['line']}"
        emails.append(mail(
            f"Hi {sup_contact},\n\nSmall correction: the letter is missing our unit number. The billing "
            f"address is {label_addr['line']}. Invoices to {billing}, please.\n\nThanks,{sig}",
            "Address correction", day))
    else:  # variant 11: AP contact given only in a forwarded signature block
        billing = f"karen.ap@{domain}"
        emails.append(mail(
            f"Hi {sup_contact},\n\nLooping in our payables lead, details below. She'll want your "
            f"invoices each month.\n\n---\nKaren\nPayables Lead\n{billing}\n{sig}", "Intro to Karen", day))

    text = contract_text(supplier["name"], legal, addr, "operations", "Fees as set out in Schedule A.",
                         "net 30 days from the invoice date", trade_name=trade, prov_full=prov_full)
    label = {"legal_name": label_legal, "billing_email": billing, "province": label_addr["province"],
             "address": label_addr["line"], "po_number": po, "split_invoices": False}
    return {"legal": legal, "short": short, "domain": domain, "contact": contact, "address": addr,
            "contract_text": text, "emails": emails, "label": label}


def write_eml(path: Path, m: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_eml(sender=m["sender"], to=m["to"], subject=m["subject"],
                               date=m["date"], body=m["body"]))


def weekdays(year: int, month: int, last_day: int) -> list[date]:
    out = []
    d = date(year, month, 1)
    while d.month == month and d.day <= last_day:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate(out: Path = OUT) -> dict:
    g = Gen()
    gst = f"{g.rng.randrange(10**8, 10**9)} RT0001"
    supplier = {
        "name": "Tamarack Lane Advisory Inc.",
        "address": "400 Example Street, Suite 210, Toronto, ON M5V 0A1",
        "province": "ON",
        "gst_hst_number": gst,
        "email": f"billing@{SUPPLIER_DOMAIN}",
        "contact_name": "Priya",
        "next_invoice_seq": 1043,
        "note": "Synthetic supplier. The GST/HST number is randomly generated and not real.",
    }
    clients, customers, contracts, timesheets, milestones, issued = [], [], [], [], [], []
    month_mail: list[tuple[str, dict]] = []
    cases: list[dict] = []
    labels: dict[str, dict] = {}
    days = weekdays(2026, 10, 23)

    def add_client(cid: str, legal: str, short: str, domain: str, prov: str, *, email=True,
                   po=None, record=True, addr=None, contact=None) -> dict:
        contact = contact or g.person(domain)
        addr = addr or g.address(prov)
        clients.append({"client_id": cid, "name": short, "domain": domain, "status": "active",
                        "primary_contact": contact["email"]})
        cust = {"client_id": cid, "legal_name": legal, "billing_email": f"ap@{domain}" if email else None,
                "province": addr["province"], "address": addr["line"], "po_number": po}
        if record:
            customers.append(cust)
        return {"contact": contact, "addr": addr, "cust": cust}

    def add_contract(ctid, cid, kind, rate, prov, terms="Net 30", *, po_required=False, cap=None,
                     end=None, desc=None, text=None, currency="CAD"):
        contracts.append({
            "contract_id": ctid, "client_id": cid, "type": kind, "rate_cents": rate, "currency": currency,
            "billing_province": prov, "payment_terms": terms, "start": "2026-01-01", "end": end,
            "po_required": po_required, "cap_cents": cap,
            "description": desc or {"hourly": "Advisory services (hourly)",
                                    "retainer": "Monthly advisory retainer",
                                    "milestone": "Fixed-fee project"}[kind],
            **({"contract_text": text} if text else {}),
        })

    def add_hours(ctid, n, rate, *, override=None):
        for i in range(n):
            role, _ = STAFF[i % len(STAFF)]
            hours = g.rng.choice([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.5])
            timesheets.append({
                "entry_id": f"T{len(timesheets) + 1:04d}", "contract_id": ctid,
                "date": g.rng.choice(days).isoformat(), "staff_role": role, "hours": hours,
                "rate_cents": override if (override and i == 0) else rate,
                "note": g.rng.choice(["Working session", "Process mapping", "Status call",
                                      "Analysis and write-up", "Stakeholder interviews", "Review of drafts"]),
            })

    # ---- existing clients ---------------------------------------------------
    def existing(prov):
        return g.company() + (prov,)

    # C001 hourly ON, clean
    legal, short, dom, prov = existing("ON")
    add_client("C001", legal, short, dom, prov)
    add_contract("K001", "C001", "hourly", 18500, prov)
    add_hours("K001", 7, 18500)

    # C002 retainer AB with some activity
    legal, short, dom, prov = existing("AB")
    add_client("C002", legal, short, dom, prov)
    add_contract("K002", "C002", "retainer", 450000, prov, "Net 15")
    add_hours("K002", 3, 0)

    # C003 retainer NS with zero activity -> flag
    legal, short, dom, prov = existing("NS")
    add_client("C003", legal, short, dom, prov)
    add_contract("K003", "C003", "retainer", 300000, prov)

    # C004 hourly BC, one entry at a rate that differs from the contract -> flag
    legal, short, dom, prov = existing("BC")
    add_client("C004", legal, short, dom, prov)
    add_contract("K004", "C004", "hourly", 17500, prov, "Net 45")
    add_hours("K004", 5, 17500, override=19500)

    # C005 milestone QC, one milestone completed in October
    legal, short, dom, prov = existing("QC")
    add_client("C005", legal, short, dom, prov)
    add_contract("K005", "C005", "milestone", 0, prov, "Due on receipt")
    milestones += [
        {"milestone_id": "M001", "contract_id": "K005", "name": "Phase 1: current-state assessment",
         "amount_cents": 1250000, "completed": "2026-09-18"},
        {"milestone_id": "M002", "contract_id": "K005", "name": "Phase 2: target operating model",
         "amount_cents": 1800000, "completed": "2026-10-16"},
        {"milestone_id": "M003", "contract_id": "K005", "name": "Phase 3: implementation plan",
         "amount_cents": 900000, "completed": None},
    ]

    # C006 hourly ON, no billing email on file -> flag
    legal, short, dom, prov = existing("ON")
    add_client("C006", legal, short, dom, prov, email=False)
    add_contract("K006", "C006", "hourly", 16500, prov)
    add_hours("K006", 3, 16500)

    # C007 hourly SK, already invoiced for the period -> duplicate flag
    legal, short, dom, prov = existing("SK")
    add_client("C007", legal, short, dom, prov)
    add_contract("K007", "C007", "hourly", 15000, prov)
    add_hours("K007", 4, 15000)
    issued.append({"number": "INV-01038", "client_id": "C007", "period": PERIOD,
                   "issue_date": "2026-10-20", "total_cents": 141750})

    # C008 hourly NB, PO required and none on file -> flag
    legal, short, dom, prov = existing("NB")
    add_client("C008", legal, short, dom, prov)
    add_contract("K008", "C008", "hourly", 16000, prov, po_required=True)
    add_hours("K008", 3, 16000)

    # C009 two contracts; client asks to split the invoice by email
    legal, short, dom, prov = existing("ON")
    c9 = add_client("C009", legal, short, dom, prov)
    add_contract("K009A", "C009", "retainer", 250000, prov, desc="Finance transformation retainer")
    add_contract("K009B", "C009", "hourly", 18500, prov, desc="Data migration support (hourly)")
    add_hours("K009B", 4, 18500)
    month_mail.append(("C009", {
        "sender": f'"{c9["contact"]["name"]}" <{c9["contact"]["email"]}>', "to": supplier["email"],
        "subject": "October invoice", "date": g.when(19),
        "body": (f"Hi Priya,\n\nCould you please split October's invoice into two: one for the finance "
                 f"transformation retainer and one for the data migration hours? They're charged to "
                 f"different cost centres on our side.\n\nThanks,\n{c9['contact']['name']}\n")}))
    labels["CHG-C009"] = {"legal_name": None, "billing_email": None, "province": None, "address": None,
                          "po_number": None, "split_invoices": True}

    # C010 hourly MB, new billing email by email
    legal, short, dom, prov = existing("MB")
    c10 = add_client("C010", legal, short, dom, prov)
    add_contract("K010", "C010", "hourly", 15500, prov)
    add_hours("K010", 5, 15500)
    new_mail = f"finance.ops@{dom}"
    month_mail.append(("C010", {
        "sender": f'"{c10["contact"]["name"]}" <{c10["contact"]["email"]}>', "to": supplier["email"],
        "subject": "Change of billing contact", "date": g.when(14),
        "body": (f"Hello,\n\nEffective immediately, please send invoices to {new_mail} instead of "
                 f"ap@{dom}. Everything else stays the same.\n\nRegards,\n{c10['contact']['name']}\n"
                 f"{c10['contact']['title']}\n")}))
    labels["CHG-C010"] = {"legal_name": None, "billing_email": new_mail, "province": None, "address": None,
                          "po_number": None, "split_invoices": False}

    # C011 retainer ON, PO required, new PO arrives by email
    legal, short, dom, prov = existing("ON")
    c11 = add_client("C011", legal, short, dom, prov, po="4500018812")
    add_contract("K011", "C011", "retainer", 375000, prov, po_required=True)
    add_hours("K011", 2, 0)
    new_po = "4500027719"
    month_mail.append(("C011", {
        "sender": f'"{c11["contact"]["name"]}" <{c11["contact"]["email"]}>', "to": supplier["email"],
        "subject": "New PO for FY2027", "date": g.when(8),
        "body": (f"Hi Priya,\n\nOur fiscal year rolled over, so PO 4500018812 is closed. The new purchase "
                 f"order number is {new_po}; please reference it on all invoices from October onward.\n\n"
                 f"Best,\n{c11['contact']['name']}\n")}))
    labels["CHG-C011"] = {"legal_name": None, "billing_email": None, "province": None, "address": None,
                          "po_number": new_po, "split_invoices": False}

    # C012 hourly ON -> moved to SK (place of supply and tax change)
    legal, short, dom, prov = existing("ON")
    c12 = add_client("C012", legal, short, dom, prov)
    add_contract("K012", "C012", "hourly", 17000, prov)
    add_hours("K012", 4, 17000)
    moved = g.address("SK")
    month_mail.append(("C012", {
        "sender": f'"{c12["contact"]["name"]}" <{c12["contact"]["email"]}>', "to": supplier["email"],
        "subject": "We've moved", "date": g.when(5),
        "body": (f"Hi all,\n\nAs of October 1 our head office and billing address has moved to:\n\n"
                 f"{moved['street']}\n{moved['city']}, {moved['province']} {moved['postal']}\n\n"
                 f"Please update your records.\n\nThanks,\n{c12['contact']['name']}\n")}))
    labels["CHG-C012"] = {"legal_name": None, "billing_email": None, "province": "SK",
                          "address": moved["line"], "po_number": None, "split_invoices": False}

    # C013 hourly contract ended in August -> not billable
    legal, short, dom, prov = existing("ON")
    add_client("C013", legal, short, dom, prov)
    add_contract("K013", "C013", "hourly", 15000, prov, end="2026-08-31")

    # C014 milestone, nothing completed this month -> not billable; a social email (noise)
    legal, short, dom, prov = existing("AB")
    c14 = add_client("C014", legal, short, dom, prov)
    add_contract("K014", "C014", "milestone", 0, prov)
    milestones.append({"milestone_id": "M004", "contract_id": "K014", "name": "Final report",
                       "amount_cents": 2200000, "completed": None})
    month_mail.append(("C014", {
        "sender": f'"{c14["contact"]["name"]}" <{c14["contact"]["email"]}>', "to": supplier["email"],
        "subject": "Thanks for Tuesday", "date": g.when(22),
        "body": ("Hi Priya,\n\nThanks for the workshop on Tuesday, the team found it really useful. "
                 "Looking forward to the final report next month.\n\nBest,\n" + c14["contact"]["name"] + "\n")}))
    labels["CHG-C014"] = {"legal_name": None, "billing_email": None, "province": None, "address": None,
                          "po_number": None, "split_invoices": False}

    # C015 small hourly job, total under $100 (CRA lowest tier)
    legal, short, dom, prov = existing("AB")
    add_client("C015", legal, short, dom, prov)
    add_contract("K015", "C015", "hourly", 15000, prov)
    timesheets.append({"entry_id": f"T{len(timesheets) + 1:04d}", "contract_id": "K015", "date": "2026-10-07",
                       "staff_role": "Consultant", "hours": 0.5, "rate_cents": 15000, "note": "Quick review call"})

    # C016 hourly with not-to-exceed cap that the month's hours exceed -> flag
    legal, short, dom, prov = existing("PE")
    add_client("C016", legal, short, dom, prov)
    add_contract("K016", "C016", "hourly", 20000, prov, cap=250000)
    for d, h in [("2026-10-05", 6.0), ("2026-10-09", 5.5), ("2026-10-15", 4.0)]:
        timesheets.append({"entry_id": f"T{len(timesheets) + 1:04d}", "contract_id": "K016", "date": d,
                           "staff_role": "Senior consultant", "hours": h, "rate_cents": 20000,
                           "note": "Pricing model build"})

    # ---- new clients this month: contracts exist, no customer record ---------
    new_variants = [0, 2, 4, 6]
    kinds = [("hourly", 18000), ("milestone", 0), ("retainer", 200000), ("hourly", 16500)]
    for i, (variant, (kind, rate)) in enumerate(zip(new_variants, kinds)):
        cid = f"N{i + 1:03d}"
        oc = onboarding_case(g, supplier, variant, 2 + i * 3)
        add_client(cid, oc["legal"], oc["short"], oc["domain"], oc["address"]["province"],
                   record=False, addr=oc["address"], contact=oc["contact"])
        ctid = f"K{cid}"
        add_contract(ctid, cid, kind, rate, oc["address"]["province"],
                     po_required=oc["label"]["po_number"] is not None or i == 2, text=oc["contract_text"])
        if kind == "hourly":
            add_hours(ctid, 4, rate)
        elif kind == "milestone":
            milestones.append({"milestone_id": f"M{len(milestones) + 1:03d}", "contract_id": ctid,
                               "name": "Discovery sprint", "amount_cents": 850000, "completed": "2026-10-21"})
        for m in oc["emails"]:
            month_mail.append((cid, m))
        cases.append({"case_id": f"NEW-{cid}", "kind": "new_client", "client_id": cid,
                      "client_name": oc["short"], "contract_text": oc["contract_text"],
                      "emails": [f"mailbox/{cid}-{j + 1}.eml" for j in range(len(oc["emails"]))]})
        labels[f"NEW-{cid}"] = oc["label"]

    # change/noise cases from the month mailbox
    counters: dict[str, int] = {}
    mail_paths = []
    for cid, m in month_mail:
        counters[cid] = counters.get(cid, 0) + 1
        rel = f"mailbox/{cid}-{counters[cid]}.eml"
        mail_paths.append((rel, m))
        if cid.startswith("C"):
            name = next(c["name"] for c in clients if c["client_id"] == cid)
            cases.append({"case_id": f"CHG-{cid}", "kind": "change", "client_id": cid, "client_name": name,
                          "contract_text": None, "emails": [rel]})

    # ---- eval-only onboarding cases (not part of the month's run) -------------
    eval_mail = []
    for i in range(16):
        variant = i % 8
        oc = onboarding_case(g, supplier, variant, 1 + (i % 20))
        cid = f"E{i + 1:03d}"
        rels = []
        for j, m in enumerate(oc["emails"]):
            rel = f"eval/mail/{cid}-{j + 1}.eml"
            eval_mail.append((rel, m))
            rels.append(rel)
        cases.append({"case_id": f"NEW-{cid}", "kind": "new_client", "client_id": None,
                      "client_name": oc["short"], "contract_text": oc["contract_text"], "emails": rels})
        labels[f"NEW-{cid}"] = oc["label"]

    # held-out phrasings (variants 8-11), plus a split request phrased without the word "split"
    for i, variant in enumerate([8, 9, 10, 11, 8, 9, 10, 11]):
        oc = onboarding_case(g, supplier, variant, 3 + i)
        cid = f"H{i + 1:03d}"
        rels = []
        for j, m in enumerate(oc["emails"]):
            rel = f"eval/mail/{cid}-{j + 1}.eml"
            eval_mail.append((rel, m))
            rels.append(rel)
        cases.append({"case_id": f"NEW-{cid}", "kind": "new_client_heldout", "client_id": None,
                      "client_name": oc["short"], "contract_text": oc["contract_text"], "emails": rels})
        labels[f"NEW-{cid}"] = oc["label"]
    _, short, dom = g.company()
    who = g.person(dom)
    eval_mail.append(("eval/mail/H009-1.eml", {
        "sender": f'"{who["name"]}" <{who["email"]}>', "to": supplier["email"], "subject": "Billing question",
        "date": g.when(20),
        "body": (f"Hi Priya,\n\nCould we get the retainer and the hourly work billed separately from this "
                 f"month? Finance wants them on different cost centres.\n\nThanks,\n{who['name']}\n")}))
    cases.append({"case_id": "CHG-H009", "kind": "change_heldout", "client_id": None, "client_name": short,
                  "contract_text": None, "emails": ["eval/mail/H009-1.eml"]})
    labels["CHG-H009"] = {"legal_name": None, "billing_email": None, "province": None, "address": None,
                          "po_number": None, "split_invoices": True}

    ops = {"generated_by": "01_invoicing/invoicing_data.py", "seed": SEED, "period": PERIOD,
           "supplier": supplier, "clients": clients, "customers": customers, "contracts": contracts,
           "timesheets": sorted(timesheets, key=lambda t: (t["contract_id"], t["date"], t["entry_id"])),
           "milestones": milestones, "issued_invoices": issued}

    # ---- write ---------------------------------------------------------------
    for sub in ("mailbox", "eval/mail"):
        d = out / sub
        if d.exists():
            for p in d.glob("*.eml"):
                p.unlink()
    for rel, m in mail_paths + eval_mail:
        write_eml(out / rel, m)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ops.json").write_text(json.dumps(ops, indent=2) + "\n")
    (out / "eval").mkdir(exist_ok=True)
    (out / "eval" / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    (out / "eval" / "labels.json").write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    return {"clients": len(clients), "cases": len(cases), "emails": len(mail_paths) + len(eval_mail)}


if __name__ == "__main__":
    print(generate())
