"""Deterministic core of the bills (accounts payable intake) agent.

Everything here is plain code with tests: totals, tax-rate checks, GST/HST number format,
input tax credit (ITC) documentary tiers, duplicate detection, due dates, payment buckets,
and the AP aging outputs. The model only ever fills in an `Extraction`.

References
- ITC documentary requirements: Input Tax Credit Information (GST/HST) Regulations, s. 3
  https://laws-lois.justice.gc.ca/eng/regulations/SOR-91-45/section-3.html
  and GST/HST Memorandum 8.4 (its dollar thresholds predate the 2021 amendment)
  https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/8-4/documentary-requirements-claiming-input-tax-credits.html
- Rates: shared/tax.py
"""

from __future__ import annotations

import calendar
import csv
import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from bills_extract import CHART_OF_ACCOUNTS, Extraction, categorize
from shared import ledger
from shared.tax import RATES, fmt
from shared.tax import gst_hst_number_check_digit_ok as bn_check_digit_ok  # noqa: F401 (re-exported for tests)
from shared.tax import gst_hst_number_format_ok as bn_format_ok
from shared.tax import normalize_gst_hst_number as normalize_bn

# ITC information tiers, by total paid or payable (cents). SOR/91-45 s. 3(a)-(c).
ITC_TIER_2 = 100_00
ITC_TIER_3 = 500_00
# ETA s. 236: 50% of the ITC on meals and entertainment is added back, so half is recoverable.
MEALS_ITC_SHARE = (1, 2)

BUCKETS = ("overdue", "due this week", "next 30 days", "later")


# --- small checks ------------------------------------------------------------------------------

def totals_match(e: Extraction, tolerance_cents: int = 1) -> bool:
    """subtotal + GST/HST + other provincial tax = total, within a cent of line-level rounding."""
    if e.subtotal_cents is None or e.total_cents is None:
        return False
    return abs(e.subtotal_cents + (e.tax_cents or 0) + (e.other_tax_cents or 0) - e.total_cents) <= tolerance_cents


def expected_tax(subtotal_cents: int, rate: float) -> int:
    return int(subtotal_cents * rate + 0.5)


def tax_rate_check(e: Extraction, recipient_province: str) -> tuple[bool, str]:
    """Is the GST/HST charged consistent with a Canadian rate?

    Place-of-supply rules mean a supplier normally charges the rate of the province where the
    supply is made, which for most services and shipped goods is the recipient's province. So a
    rate is accepted if it matches either the vendor's province or the recipient's province.
    """
    sub, tax = e.subtotal_cents, e.tax_cents or 0
    if sub is None or sub == 0:
        return True, "no subtotal to test"
    if e.vendor_country and e.vendor_country != "CA":
        if tax == 0:
            return True, "non-resident vendor, no GST/HST charged"
        return False, f"non-resident vendor charged {tax / sub:.2%} GST/HST; confirm it is registered"
    if tax == 0:
        return True, "no GST/HST charged (zero-rated, exempt or small supplier)"
    candidates = {p for p in (e.vendor_province, recipient_province) if p in RATES}
    for p in candidates:
        if abs(expected_tax(sub, RATES[p][0]) - tax) <= 2:
            return True, f"{RATES[p][0]:.0%} matches {p}"
    if not e.vendor_province:
        return False, f"{tax / sub:.2%} charged and vendor province unknown"
    want = " or ".join(f"{p} {RATES[p][0]:.0%}" for p in sorted(candidates))
    return False, f"{tax / sub:.2%} charged; expected {want}"


@dataclass
class ITC:
    status: str              # eligible | not_eligible | not_applicable | needs_review
    tier: str
    missing: list[str]
    claimable_cents: int


def itc_assessment(e: Extraction, category: str) -> ITC:
    """Apply the SOR/91-45 s. 3 information tiers to decide whether an ITC can be claimed.

    Assumes the purchase is 100% for commercial activities. The thresholds apply to the total
    paid or payable on the document.
    """
    tax = e.tax_cents or 0
    if tax == 0:
        return ITC("not_applicable", "-", [], 0)
    total = e.total_cents or 0
    missing: list[str] = []
    if not e.vendor_name:
        missing.append("supplier name")
    if not e.invoice_date:
        missing.append("invoice date")
    if not e.total_cents:
        missing.append("total amount")
    tier = "under $100"
    if total >= ITC_TIER_2:
        tier = "$100 to $499.99"
        if not bn_format_ok(e.gst_number):
            missing.append("valid GST/HST registration number")
        if e.tax_cents is None:
            missing.append("tax amount")
    if total >= ITC_TIER_3:
        tier = "$500 or more"
        if not e.recipient_name:
            missing.append("recipient name")
        if not e.terms:
            missing.append("terms of payment")
        if not e.description:
            missing.append("description of supply")
    if e.currency != "CAD":
        missing.append("CAD amount of tax (foreign-currency invoice)")
    if missing:
        return ITC("not_eligible", tier, missing, 0)
    claim = tax
    if category == "meals":
        n, d = MEALS_ITC_SHARE
        claim = (tax * n + d - 1) // d
    return ITC("eligible", tier, [], claim)


# --- terms and buckets -------------------------------------------------------------------------

def parse_terms(terms: str | None, default_days: int = 30) -> tuple[int | str, int | None, bool]:
    """Return (net days or 'EOM', early-discount days or None, assumed?)."""
    if not terms:
        return default_days, None, True
    t = terms.lower()
    if re.search(r"due (on|upon) receipt|on receipt|cod\b|immediate", t):
        return 0, None, False
    if re.search(r"\beom\b|end of month", t):
        return "EOM", None, False
    disc = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)", t)
    net = re.search(r"net\s*(\d+)|(\d+)\s*days", t)
    if net:
        days = int(net.group(1) or net.group(2))
        return days, (int(disc.group(2)) if disc else None), False
    return default_days, None, True


def due_date(invoice_date: date, terms: str | None, default_days: int = 30) -> tuple[date, date | None, bool]:
    days, disc, assumed = parse_terms(terms, default_days)
    if days == "EOM":
        last = calendar.monthrange(invoice_date.year, invoice_date.month)[1]
        return invoice_date.replace(day=last), None, False
    discount_by = invoice_date + timedelta(days=disc) if disc else None
    return invoice_date + timedelta(days=days), discount_by, assumed


def bucket(due: date, as_of: date) -> str:
    if due < as_of:
        return "overdue"
    if due <= as_of + timedelta(days=7):
        return "due this week"
    if due <= as_of + timedelta(days=30):
        return "next 30 days"
    return "later"


# --- duplicates --------------------------------------------------------------------------------

_SUFFIX = re.compile(r"\b(inc|ltd|llp|llc|corp|co|corporation|limited|incorporated)\b\.?")


def norm_vendor(name: str | None) -> str:
    n = _SUFFIX.sub("", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def norm_number(num: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (num or "").upper()).lstrip("0")


def find_duplicate(e: Extraction, earlier: list[tuple[str, Extraction]],
                   amount_tolerance: float = 0.01, days: int = 7) -> tuple[str, str] | None:
    """Return (kind, source) if `e` repeats an earlier bill.

    exact: same vendor and invoice number. fuzzy: similar vendor name, total within 1% and
    invoice dates within a week, under a different number (a re-issue or double entry).
    """
    v, n = norm_vendor(e.vendor_name), norm_number(e.invoice_number)
    for src, o in earlier:
        if v and v == norm_vendor(o.vendor_name) and n and n == norm_number(o.invoice_number):
            return "exact", src
    for src, o in earlier:
        if not (e.total_cents and o.total_cents and e.invoice_date and o.invoice_date):
            continue
        similar = difflib.SequenceMatcher(None, v, norm_vendor(o.vendor_name)).ratio() >= 0.85
        close_amt = abs(e.total_cents - o.total_cents) <= max(1, amount_tolerance * o.total_cents)
        gap = abs((date.fromisoformat(e.invoice_date) - date.fromisoformat(o.invoice_date)).days)
        if similar and close_amt and gap <= days:
            return "fuzzy", src
    return None


def ledger_bills(con) -> list[tuple[str, Extraction]]:
    """Bills already in the ledger, as extractions, so a re-run catches bills posted earlier."""
    rows = con.execute(
        "select b.number, b.bill_date, b.subtotal_cents, b.tax_cents, b.other_tax_cents, b.total_cents,"
        " b.currency, b.source, v.name from bills b join vendors v on v.id = b.vendor_id").fetchall()
    return [(f"ledger:{r['source']}", Extraction(
        doc_type="bill", vendor_name=r["name"], invoice_number=r["number"], invoice_date=r["bill_date"],
        subtotal_cents=r["subtotal_cents"], tax_cents=r["tax_cents"], other_tax_cents=r["other_tax_cents"] or 0,
        currency=r["currency"] or "CAD",
        total_cents=r["total_cents"] if r["total_cents"] is not None else
        (r["subtotal_cents"] or 0) + (r["tax_cents"] or 0) + (r["other_tax_cents"] or 0))) for r in rows]


# --- the review record ------------------------------------------------------------------------

@dataclass
class BillReview:
    source: str
    extraction: Extraction
    category: str
    itc: ITC
    due: date | None
    discount_by: date | None
    bucket: str | None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duplicate_of: str | None = None

    @property
    def e(self) -> Extraction:
        return self.extraction

    def summary(self) -> str:
        e = self.e
        return (f"{e.vendor_name or '?'} #{e.invoice_number or '?'} {e.invoice_date or '?'} "
                f"{e.currency} {fmt(e.total_cents or 0)} due {self.due or '?'} [{self.category}]")

    def payload(self) -> dict:
        e = self.e
        return {
            "source": self.source, "vendor": e.vendor_name, "gst_number": normalize_bn(e.gst_number),
            "number": e.invoice_number, "bill_date": e.invoice_date,
            "due_date": self.due.isoformat() if self.due else None,
            "currency": e.currency, "subtotal_cents": e.subtotal_cents, "tax_cents": e.tax_cents,
            "other_tax_cents": e.other_tax_cents, "total_cents": e.total_cents,
            "itc_status": self.itc.status, "itc_claimable_cents": self.itc.claimable_cents,
            "category": self.category, "account": CHART_OF_ACCOUNTS[self.category][0],
            "terms": e.terms, "bucket": self.bucket,
        }


def review_bill(source: str, e: Extraction, *, as_of: date, recipient_province: str,
                earlier: list[tuple[str, Extraction]]) -> BillReview:
    flags: list[str] = []
    notes: list[str] = list(e.notes)
    category = e.category if e.category in CHART_OF_ACCOUNTS else categorize(
        f"{e.vendor_name or ''} {e.description or ''}")

    missing = [k for k in ("vendor_name", "invoice_number", "invoice_date", "total_cents")
               if getattr(e, k) in (None, "")]
    if missing:
        flags.append(f"missing fields: {', '.join(missing)}")

    if not totals_match(e):
        flags.append(f"total mismatch: subtotal {fmt(e.subtotal_cents or 0)} + tax "
                     f"{fmt((e.tax_cents or 0) + (e.other_tax_cents or 0))} != total {fmt(e.total_cents or 0)}")

    ok, why = tax_rate_check(e, recipient_province)
    if not ok:
        flags.append(f"tax rate inconsistent with vendor address: {why}")
    else:
        notes.append(f"tax rate: {why}")

    if e.currency != "CAD":
        flags.append(f"foreign currency: {e.currency}; convert at the Bank of Canada rate before posting")

    tax = e.tax_cents or 0
    if e.gst_number and not bn_format_ok(e.gst_number):
        flags.append(f"GST/HST number '{e.gst_number}' is not 9 digits + RT + 4 digits")
    elif e.gst_number and not bn_check_digit_ok(e.gst_number):
        flags.append(f"GST/HST number '{e.gst_number}' fails the check digit; confirm in the CRA registry")
    elif not e.gst_number and tax > 0 and (e.total_cents or 0) >= ITC_TIER_2:
        flags.append(f"missing GST/HST registration number on a {fmt(e.total_cents or 0)} claim (over $100)")

    itc = itc_assessment(e, category)
    if itc.status == "eligible" and (not ok or not totals_match(e)):
        # Tax charged in error is not tax payable; hold the claim until the vendor corrects it.
        itc = ITC("needs_review", itc.tier, ["confirm the GST/HST amount with the vendor"], 0)
    if itc.status == "eligible" and category == "meals":
        notes.append("meals: 50% of the tax is claimable")

    dup = find_duplicate(e, earlier)
    dup_of = None
    if dup:
        kind, src = dup
        dup_of = src
        flags.append("duplicate: same vendor and invoice number as " + src if kind == "exact"
                     else "possible duplicate: same vendor, amount and dates as " + src)

    due = disc = None
    bkt = None
    if e.invoice_date:
        due, disc, assumed = due_date(date.fromisoformat(e.invoice_date), e.terms)
        if assumed:
            notes.append("terms not found; assumed Net 30")
        bkt = bucket(due, as_of)
    return BillReview(source, e, category, itc, due, disc, bkt, flags, notes, dup_of)


def process(items: list[tuple[str, Extraction]], *, as_of: date, recipient_province: str = "ON",
            existing: list[tuple[str, Extraction]] = ()) -> list[BillReview]:
    """Review every bill in mailbox order. Non-bills are ignored here."""
    earlier = list(existing)
    out = []
    for src, e in items:
        if e.doc_type != "bill":
            continue
        r = review_bill(src, e, as_of=as_of, recipient_province=recipient_province, earlier=earlier)
        out.append(r)
        earlier.append((src, e))
    return out


# --- approvals and ledger ----------------------------------------------------------------------

def propose_all(queue, reviews: list[BillReview]) -> None:
    for r in reviews:
        queue.propose("bill", f"{r.source}: {r.summary()}", r.payload(), flags=r.flags)


def post_approved(target, approved_payloads: list[dict]) -> list:
    """Write approved bills to the ledger. Only called after a person (or auto-review) approves.

    `target` is a sqlite3 connection or any shared.protocols.LedgerAdapter (QuickBooks, Xero)."""
    led = ledger.as_adapter(target)
    ids = []
    for p in approved_payloads:
        days, _, _ = parse_terms(p.get("terms"))
        vid = led.upsert_vendor(p["vendor"], gst_number=p["gst_number"] or "",
                                terms_days=days if isinstance(days, int) else 30)
        ids.append(led.create_bill(
            vendor_id=vid, number=p["number"], bill_date=p["bill_date"], due_date=p["due_date"],
            subtotal_cents=p["subtotal_cents"], tax_cents=p["tax_cents"],
            other_tax_cents=p.get("other_tax_cents") or 0, total_cents=p.get("total_cents"),
            currency=p.get("currency") or "CAD", itc_eligible=1 if p["itc_status"] == "eligible" else 0, category=p["category"],
            status="approved", source=p["source"]))
    led.commit()
    return ids


# --- AP aging outputs --------------------------------------------------------------------------

AGING_FIELDS = ["source", "vendor", "invoice_number", "invoice_date", "due_date", "bucket", "currency",
                "subtotal", "gst_hst", "other_tax", "total", "itc_status", "itc_claimable", "category",
                "decision", "flags"]


def aging_rows(reviews: list[BillReview], decisions: dict[str, str]) -> list[dict]:
    rows = []
    for r in reviews:
        e = r.e
        rows.append({
            "source": r.source, "vendor": e.vendor_name, "invoice_number": e.invoice_number,
            "invoice_date": e.invoice_date, "due_date": r.due.isoformat() if r.due else "",
            "bucket": r.bucket or "", "currency": e.currency,
            "subtotal": f"{(e.subtotal_cents or 0) / 100:.2f}", "gst_hst": f"{(e.tax_cents or 0) / 100:.2f}",
            "other_tax": f"{(e.other_tax_cents or 0) / 100:.2f}", "total": f"{(e.total_cents or 0) / 100:.2f}",
            "itc_status": r.itc.status, "itc_claimable": f"{r.itc.claimable_cents / 100:.2f}",
            "category": r.category, "decision": decisions.get(r.source, "pending"),
            "flags": "; ".join(r.flags),
        })
    return rows


def bucket_totals(reviews: list[BillReview]) -> dict[str, dict[str, int]]:
    """Totals in cents by currency and bucket. Exact duplicates are left out so nothing counts twice."""
    out: dict[str, dict[str, int]] = {}
    for r in reviews:
        if any(f.startswith("duplicate:") for f in r.flags) or not r.bucket:
            continue
        cur = out.setdefault(r.e.currency, {b: 0 for b in BUCKETS})
        cur[r.bucket] += r.e.total_cents or 0
    return out


def write_aging(reviews: list[BillReview], decisions: dict[str, str], out_dir: Path, as_of: date) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = aging_rows(reviews, decisions)
    csv_path = out_dir / "ap_aging.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=AGING_FIELDS)
        w.writeheader()
        w.writerows(rows)

    totals = bucket_totals(reviews)
    md = [f"# AP aging summary as of {as_of.isoformat()}", "",
          "Proposed bills from the finance mailbox. Nothing is posted until approved.", "",
          "| Currency | " + " | ".join(BUCKETS) + " | Total |", "|---" * (len(BUCKETS) + 2) + "|"]
    for cur, b in sorted(totals.items()):
        md.append(f"| {cur} | " + " | ".join(fmt(b[k]) for k in BUCKETS) + f" | {fmt(sum(b.values()))} |")
    itc_total = sum(r.itc.claimable_cents for r in reviews
                    if not any(f.startswith("duplicate:") for f in r.flags))
    md += ["", f"ITC claimable on eligible bills: {fmt(itc_total)}", "",
           "Exact duplicates are listed below but left out of the totals.", "",
           "| Bucket | Due | Vendor | Invoice | Total | ITC | Category | Decision | Flags |",
           "|---|---|---|---|---|---|---|---|---|"]
    order = {b: i for i, b in enumerate(BUCKETS)}
    for r, row in sorted(zip(reviews, rows), key=lambda t: (order.get(t[0].bucket or "later", 9), t[1]["due_date"])):
        md.append(f"| {row['bucket']} | {row['due_date']} | {row['vendor']} | {row['invoice_number']} | "
                  f"{row['currency']} {row['total']} | {row['itc_status']} {row['itc_claimable']} | "
                  f"{row['category']} | {row['decision']} | {row['flags'] or '-'} |")
    md_path = out_dir / "ap_aging.md"
    md_path.write_text("\n".join(md) + "\n")
    return csv_path, md_path
