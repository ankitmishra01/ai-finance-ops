"""The model step of the bills agent: read one email, say what it is, and pull the bill fields.

Two interchangeable extractors implement `Extractor.extract(mail) -> Extraction`:

* `RulesExtractor` is a deterministic regex/heuristic baseline. It runs in tests and CI.
* `ClaudeExtractor` sends the same email text to Claude through `shared.llm.extract`, which
  forces a single JSON-schema tool call. It is used with `--extractor claude`, or automatically
  when AFO_LLM_MODE is `live` or `record`.

Neither extractor does arithmetic or tax logic beyond reading numbers off the page; checks
live in `bills.py`.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from shared import llm
from shared.mail import Mail

MODULE_DIR = Path(__file__).resolve().parent
FIXTURES = MODULE_DIR / "fixtures"
DOC_TYPES = ("bill", "statement", "receipt", "other")

# Small chart of accounts for expense suggestions. Codes are illustrative.
CHART_OF_ACCOUNTS = {
    "software": ("5100", "Software and subscriptions"),
    "office": ("5200", "Office supplies and equipment"),
    "professional_fees": ("5300", "Professional fees"),
    "rent": ("5400", "Rent"),
    "utilities": ("5500", "Utilities and telecom"),
    "travel": ("5600", "Travel"),
    "advertising": ("5700", "Advertising and marketing"),
    "repairs": ("5800", "Repairs, maintenance and cleaning"),
    "meals": ("5900", "Meals and entertainment"),
    "shipping": ("5950", "Courier and shipping"),
    "general": ("6000", "General and administrative"),
}

PROVINCES = ("ON", "QC", "NS", "NB", "MB", "BC", "PE", "SK", "AB", "NL", "YT", "NT", "NU")


@dataclass
class Extraction:
    """What an extractor read from one email. Money is integer cents; dates are ISO strings."""

    doc_type: str
    vendor_name: str | None = None
    vendor_province: str | None = None
    vendor_country: str | None = None
    gst_number: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    subtotal_cents: int | None = None
    tax_cents: int | None = None          # GST/HST only, the part that can be an ITC
    other_tax_cents: int = 0              # PST/RST/QST, not recoverable as an ITC
    total_cents: int | None = None
    currency: str = "CAD"
    terms: str | None = None
    recipient_name: str | None = None
    description: str | None = None
    category: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Extractor(Protocol):
    name: str

    def extract(self, mail: Mail) -> Extraction: ...


# --- shared text helpers -----------------------------------------------------------------------

def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # a corrupt attachment should not stop the batch
        return f"[unreadable PDF: {exc.__class__.__name__}]"


def mail_documents(mail: Mail) -> list[tuple[str, str]]:
    """(label, text) for the body and each readable attachment."""
    docs = [("email body", mail.body or "")]
    for a in mail.attachments:
        if a.content_type == "application/pdf" or a.filename.lower().endswith(".pdf"):
            docs.append((f"attachment {a.filename}", pdf_text(a.data)))
        elif a.content_type.startswith("text/"):
            docs.append((f"attachment {a.filename}", a.data.decode("utf-8", "replace")))
    return docs


def to_cents(s: str | float | int | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        d = Decimal(str(s).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return int((d * 100).quantize(Decimal("1")))


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RX = (r"(\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]{3,9}\.? \d{4})")


def parse_date(s: str | None) -> str | None:
    """ISO, 'October 5, 2026', 'Oct 5 2026' or '5 Oct 2026' to ISO. Numeric d/m/y is refused (ambiguous)."""
    if not s:
        return None
    s = s.strip().replace(".", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return None
    m = re.fullmatch(r"([A-Za-z]+) (\d{1,2}),? (\d{4})", s) or None
    if m:
        mon, d, y = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", s)
        if not m:
            return None
        d, mon, y = m.group(1), m.group(2), m.group(3)
    mi = MONTHS.get(mon[:3].lower())
    if not mi:
        return None
    try:
        return date(int(y), mi, int(d)).isoformat()
    except ValueError:
        return None


# --- rules baseline ----------------------------------------------------------------------------

AMOUNT = r"\$?\s?(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})"
AMOUNT_ONLY = re.compile(rf"^\s*(?:US)?{AMOUNT}\s*$")

CATEGORY_KEYWORDS = [
    ("meals", r"catering|lunch|dinner|coffee|pastr|restaurant|meal"),
    ("rent", r"\brent\b|lease|tenant"),
    ("software", r"software|subscription|saas|hosting|\bplan\b|seats|storage|licen[cs]e|analytics"),
    ("utilities", r"telecom|phone|internet|hydro|electric|utilit"),
    ("professional_fees", r"accounting|bookkeeping|legal|advisory|consulting|audit"),
    ("advertising", r"campaign|advertis|marketing|brochure|\bprint(?:ing|ed)?\b|decal|\bsign"),
    ("repairs", r"repair|maintenance|cleaning|renovat|janitor"),
    ("shipping", r"courier|delivery|shipping|freight"),
    ("office", r"paper|toner|office supply|shelving|furniture|chair|stationery"),
    ("travel", r"flight|hotel|airfare|taxi|mileage"),
]


def categorize(text: str) -> str:
    t = text.lower()
    for cat, rx in CATEGORY_KEYWORDS:
        if re.search(rx, t):
            return cat
    return "general"


def _merge_amount_lines(text: str) -> list[str]:
    """pypdf often puts a right-aligned amount on its own line; glue it back onto its label."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if out and AMOUNT_ONLY.match(line) and not AMOUNT_ONLY.match(out[-1]):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


def _amount_on(line: str) -> int | None:
    found = re.findall(AMOUNT, line)
    return to_cents(found[-1]) if found else None


class RulesExtractor:
    """Regex and keyword heuristics. Fast, free, and brittle on layouts it has not seen."""

    name = "rules"

    def classify(self, mail: Mail, docs: list[tuple[str, str]]) -> str:
        subject = (mail.subject or "").lower()
        heads = " ".join(" ".join(t.lower().split("\n")[:6]) for _, t in docs)
        everything = " ".join(t.lower() for _, t in docs)
        if re.search(r"statement of account|account statement|\bstatement\b", subject + " " + heads):
            return "statement"
        if re.search(r"payment received|payment receipt|receipt for your payment|paid in full|"
                     r"thank you for your payment|^paid:", subject + "\n" + heads):
            return "receipt"
        has_total = any(re.search(rf"(total|amount due|balance due)[^\n]*{AMOUNT}", ln)
                        for ln in _merge_amount_lines(everything))
        if re.search(r"invoice|\bbill\b", subject + " " + everything) and has_total:
            return "bill"
        return "other"

    def extract(self, mail: Mail) -> Extraction:
        docs = mail_documents(mail)
        doc_type = self.classify(mail, docs)
        if doc_type != "bill":
            return Extraction(doc_type=doc_type)
        # Prefer the attachment (the invoice proper); fall back to the body.
        label, text = docs[-1] if len(docs) > 1 else docs[0]
        lines = _merge_amount_lines(text)
        e = Extraction(doc_type="bill", notes=[f"read from {label}"])
        e.vendor_name = self._vendor(mail, lines)
        e.vendor_province, e.vendor_country = self._vendor_region(lines)
        e.gst_number = self._first(lines, r"(?:gst|hst|gst/hst|business)\s*(?:reg(?:istration)?\.?)?\s*"
                                          r"(?:#|no\.?|number)?\s*:?\s*(\d[\d ]{5,}\s*RT\s*\d+)")
        if e.gst_number:
            e.gst_number = re.sub(r"\s+", "", e.gst_number)
        e.invoice_number = self._first(lines, r"(?:invoice|bill)\s*(?:#|no\.?|number)\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)")
        e.invoice_date = parse_date(self._first(lines, rf"(?:invoice date|issued|date)\s*:?\s*{DATE_RX}"))
        e.terms = self._terms(lines)
        e.recipient_name = self._recipient(lines)
        e.currency = "USD" if re.search(r"\bUSD\b|US\$", text) else "CAD"
        self._amounts(lines, e)
        items = [ln for ln in lines if _amount_on(ln) is not None
                 and not re.search(r"total|tax|gst|hst|pst|qst|amount|balance", ln, re.I)]
        e.description = "; ".join(re.sub(rf"\s*\.*\s*{AMOUNT}\s*$", "", ln) for ln in items) or None
        e.category = categorize(f"{e.vendor_name or ''} {e.description or ''}")
        return e

    @staticmethod
    def _first(lines: list[str], rx: str) -> str | None:
        for ln in lines:
            m = re.search(rx, ln, re.I)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _vendor(mail: Mail, lines: list[str]) -> str | None:
        for ln in lines:
            m = re.match(r"(?:vendor|supplier|from)\s*:\s*(.+)", ln, re.I)
            if m:
                return m.group(1).strip()
        # Layouts with a title first keep the vendor in the footer, just above its address line.
        for i, ln in enumerate(lines):
            if i > 0 and re.search(r"\|.*,\s*[A-Z]{2}\s", ln):
                return lines[i - 1]
        first = lines[0] if lines else ""
        if first and not re.search(r"invoice", first, re.I):
            return first.title() if first.isupper() else first
        display = mail.sender.split("<")[0].strip().strip('"')
        if display and not re.search(r"billing|accounts|receivable|team|noreply", display, re.I):
            return display
        return None

    @staticmethod
    def _vendor_region(lines: list[str]) -> tuple[str | None, str | None]:
        skip = 0
        for ln in lines:
            if re.match(r"bill to", ln, re.I):
                skip = 3  # the recipient block follows "Bill to"
                continue
            if skip:
                skip -= 1
                continue
            m = re.search(rf",\s*({'|'.join(PROVINCES)})\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d", ln)
            if m:
                return m.group(1), "CA"
            if re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", ln):
                return None, "US"
        return None, None

    @staticmethod
    def _terms(lines: list[str]) -> str | None:
        for ln in lines:
            m = re.search(r"(?:payment terms|terms)\s*:?\s*(.+)", ln, re.I)
            if m:
                return m.group(1).strip()
        for ln in lines:
            m = re.search(r"(due (?:on|upon) receipt|\d+/\d+,? net \d+|net \d+)", ln, re.I)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _recipient(lines: list[str]) -> str | None:
        for i, ln in enumerate(lines):
            m = re.match(r"bill(?:ed)? to\s*:?\s*(.*)", ln, re.I)
            if m:
                return m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else None)
        return None

    @staticmethod
    def _amounts(lines: list[str], e: Extraction) -> None:
        for ln in lines:
            low = ln.lower()
            amt = _amount_on(ln)
            if amt is None:
                continue
            if re.match(r"(sub-?total|amount before tax|pre-tax)", low):
                e.subtotal_cents = amt
            elif re.match(r"(pst|rst|qst)\b", low):
                e.other_tax_cents += amt
            elif re.match(r"(hst|gst|gst/hst|sales tax|tax)\b", low) and not re.search(r"reg|#|no\.", low):
                e.tax_cents = (e.tax_cents or 0) + amt
            elif re.match(r"(total|amount due|balance due|amount payable)", low):
                e.total_cents = amt
        if e.tax_cents is None and e.subtotal_cents is not None:
            e.tax_cents = 0  # no tax line printed


# --- Claude ------------------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are the accounts-payable intake clerk for a small Canadian business. Each message
comes from the shared finance mailbox. You read it and return one structured record. You do
not decide whether to pay, and you do not do arithmetic checks; other code does that.

1. Classify the message as exactly one doc_type:
   - "bill": a supplier invoice that asks this business to pay a specific amount for goods or
     services. It has an invoice or bill number and a total owing. It may be a PDF attachment or
     typed into the email body. A resend of an invoice is still a bill.
   - "statement": a statement of account or ledger listing several invoices, payments and a running
     balance. It summarises bills that were already sent; it is not itself a bill.
   - "receipt": confirmation that a payment was received or an invoice was paid.
   - "other": anything else: marketing, newsletters, webinars, personal notes, and portal notices
     that say an invoice exists but do not contain its amounts.
   For anything other than "bill", set every bill field to null.

2. For bills, copy values exactly as printed on the invoice. Do not compute missing values.
   - vendor_name: the supplier's business name as printed (not the email display name such as
     "Accounts Receivable" or "Billing Team", and not the "Bill to" customer).
   - vendor_province: two-letter code from the supplier's own address. null if outside Canada or
     not shown. vendor_country: "CA", "US" or another ISO code.
   - gst_number: the supplier's GST/HST registration number exactly as printed, including "RT" and
     the account digits, spaces removed. null if none is printed. Never invent or repair one.
   - invoice_number, invoice_date (YYYY-MM-DD; refuse to guess day/month order on numeric dates,
     return null instead), terms (as printed, e.g. "Net 30", "Due on receipt", "2/10 Net 30").
   - subtotal: amount before tax. gst_hst: the GST or HST amount only. other_tax: PST, RST or QST
     (0 if none). total: the amount due as printed, even if it does not add up.
     Amounts are plain decimal strings like "1234.50" with no currency symbol or commas.
   - currency: ISO code. Use "USD" only if the invoice says USD or US$; otherwise "CAD".
   - recipient_name: the "Bill to" customer name, or null.
   - description: a short summary of what was supplied.
   - category: one of {list(CHART_OF_ACCOUNTS)}. Meals and catering for staff or clients are
     "meals"; cleaning is "repairs"; hosting and SaaS are "software".

3. If something looks wrong (for example a total that does not equal subtotal plus tax, or a
   registration number with the wrong number of digits), still copy what is printed and describe
   it in "notes"."""

_nullable_str = {"type": ["string", "null"]}
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "doc_type_reason": {"type": "string", "description": "One sentence on why."},
        "vendor_name": _nullable_str,
        "vendor_province": {"type": ["string", "null"], "enum": [*PROVINCES, None]},
        "vendor_country": _nullable_str,
        "gst_number": _nullable_str,
        "invoice_number": _nullable_str,
        "invoice_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "terms": _nullable_str,
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code"},
        "subtotal": {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d{1,2})?$"},
        "gst_hst": {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d{1,2})?$"},
        "other_tax": {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d{1,2})?$"},
        "total": {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d{1,2})?$"},
        "recipient_name": _nullable_str,
        "description": _nullable_str,
        "category": {"type": ["string", "null"], "enum": [*CHART_OF_ACCOUNTS, None]},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_type", "doc_type_reason", "vendor_name", "gst_number", "invoice_number",
                 "invoice_date", "subtotal", "gst_hst", "total", "currency", "terms", "category"],
}


def render_prompt(mail: Mail) -> str:
    parts = [f"From: {mail.sender}", f"Subject: {mail.subject}", f"Date: {mail.date}", ""]
    for label, text in mail_documents(mail):
        parts += [f"<{label}>", text.strip()[:12000], f"</{label}>", ""]
    return "\n".join(parts)


class ClaudeExtractor:
    name = "claude"

    def __init__(self, fixtures_dir: Path = FIXTURES) -> None:
        self.fixtures_dir = fixtures_dir

    def extract(self, mail: Mail) -> Extraction:
        out = llm.extract(module="bills", task="classify_extract", system=SYSTEM_PROMPT,
                          prompt=render_prompt(mail), schema=EXTRACTION_SCHEMA,
                          fixtures_dir=self.fixtures_dir)
        return from_model_output(out)


def from_model_output(out: dict) -> Extraction:
    doc_type = out.get("doc_type") if out.get("doc_type") in DOC_TYPES else "other"
    if doc_type != "bill":
        return Extraction(doc_type=doc_type, notes=list(out.get("notes") or []))
    gst = out.get("gst_number")
    return Extraction(
        doc_type="bill",
        vendor_name=out.get("vendor_name"),
        vendor_province=out.get("vendor_province"),
        vendor_country=out.get("vendor_country"),
        gst_number=re.sub(r"\s+", "", gst) if gst else None,
        invoice_number=out.get("invoice_number"),
        invoice_date=parse_date(out.get("invoice_date")),
        subtotal_cents=to_cents(out.get("subtotal")),
        tax_cents=to_cents(out.get("gst_hst")),
        other_tax_cents=to_cents(out.get("other_tax")) or 0,
        total_cents=to_cents(out.get("total")),
        currency=(out.get("currency") or "CAD").upper(),
        terms=out.get("terms"),
        recipient_name=out.get("recipient_name"),
        description=out.get("description"),
        category=out.get("category") if out.get("category") in CHART_OF_ACCOUNTS else None,
        notes=list(out.get("notes") or []),
    )


def get_extractor(name: str | None = None) -> Extractor:
    if name == "claude" or (name is None and os.environ.get("AFO_LLM_MODE") in ("live", "record")):
        return ClaudeExtractor()
    return RulesExtractor()
