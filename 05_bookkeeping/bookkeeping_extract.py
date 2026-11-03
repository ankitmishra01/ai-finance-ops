"""The two model steps of the bookkeeping agent, each behind an Extractor.

1. read_receipt: turn one email (body + any PDF/text attachment) into structured
   receipt fields.
2. categorise: pick an account from the chart of accounts for one bank/card line,
   with a confidence and a short reason.

RulesExtractor is a deterministic baseline (regex + keyword scoring) used in tests
and CI. ClaudeExtractor sends the same inputs to shared.llm.extract with a
schema-checked JSON answer. Neither does any arithmetic that matters downstream: matching, tax
checks and filing are plain code in bookkeeping_match.py and bookkeeping.py.
"""

from __future__ import annotations

import io
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import llm  # noqa: E402
from shared.mail import Mail  # noqa: E402

MODULE = "bookkeeping"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LOW_CONFIDENCE = 0.6


# ---------------------------------------------------------------- inputs

@dataclass
class ReceiptDoc:
    """One email from the receipts mailbox, with attachment text already pulled out."""
    receipt_id: str
    path: Path
    sender: str
    subject: str
    sent: str
    body: str
    attachment_texts: list[tuple[str, str]] = field(default_factory=list)   # (filename, text)
    evidence_name: str = ""          # file to keep as evidence (attachment name, or the .eml)
    evidence_bytes: bytes = b""

    @property
    def text(self) -> str:
        parts = [f"Subject: {self.subject}", self.body]
        parts += [f"[attachment {n}]\n{t}" for n, t in self.attachment_texts]
        return "\n".join(parts)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def doc_from_mail(m: Mail) -> ReceiptDoc:
    texts, ev_name, ev_bytes = [], m.path.name, m.path.read_bytes()
    for a in m.attachments:
        if a.content_type == "application/pdf":
            texts.append((a.filename, pdf_text(a.data)))
        elif a.content_type.startswith("text/"):
            texts.append((a.filename, a.data.decode("utf-8", "replace")))
        else:
            continue
        if ev_name == m.path.name:          # first readable attachment is the document of record
            ev_name, ev_bytes = a.filename, a.data
    return ReceiptDoc(m.path.stem, m.path, m.sender, m.subject, m.date, m.body, texts, ev_name, ev_bytes)


# ---------------------------------------------------------------- schemas and prompts

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean",
                       "description": "True for a receipt, invoice, bill, folio or refund confirmation. "
                                      "False for quotes, estimates, marketing and anything that is not "
                                      "evidence of a completed purchase or refund."},
        "merchant": {"type": "string", "description": "Supplier's trading name, not the payment processor."},
        "date": {"type": "string", "description": "Transaction or invoice date as YYYY-MM-DD."},
        "currency": {"type": "string", "enum": ["CAD", "USD", "EUR", "GBP", "OTHER"]},
        "subtotal": {"type": ["number", "null"], "description": "Before tax and tip."},
        "gst_hst": {"type": "number", "description": "GST or HST charged. 0 if none shown."},
        "other_tax": {"type": "number", "description": "PST, QST, RST, municipal or other taxes. 0 if none."},
        "tip": {"type": "number", "description": "Tip or gratuity shown on the document. 0 if none."},
        "total": {"type": "number",
                  "description": "Final amount paid or payable as printed, including tax and any tip on the "
                                 "document. Negative for a refund."},
        "is_refund": {"type": "boolean"},
        "gst_number": {"type": ["string", "null"],
                       "description": "Supplier GST/HST registration number exactly as printed "
                                      "(9 digits + RT + 4 digits), or null if not printed."},
        "invoice_number": {"type": ["string", "null"]},
        "payment_date": {"type": ["string", "null"],
                         "description": "Scheduled payment or pre-authorized debit date printed on a bill, as "
                                        "YYYY-MM-DD, or null. Not the invoice date."},
        "summary": {"type": "string", "description": "What was bought, at most 15 words."},
    },
    "required": ["is_receipt", "merchant", "date", "currency", "gst_hst", "total", "is_refund", "gst_number",
                 "summary"],
}

RECEIPT_SYSTEM = """You read receipts and invoices for a small Canadian business's bookkeeping.
You get one email: its headers, body and the text of any attachments.
Return exactly the fields in the schema, read from the document. Rules:
- Copy numbers as printed. Do not compute or correct them. Amounts are plain numbers, no symbols.
- merchant is the business that supplied the goods or service. A payment processor or delivery
  platform named in the sender ("via ...", "Billing", "Receipts") is not the merchant when the
  document names the actual seller.
- date is the purchase, invoice or refund date on the document, not the email's sent date, unless
  the document shows no date. Dates like 20/10/2026 on Canadian folios are day/month/year.
- currency: "US$" or "USD" means USD. A bare "$" or "CDN$" on a Canadian supplier's document is CAD.
- gst_hst is only GST or HST. Put PST, QST, municipal accommodation tax and similar in other_tax.
- For a refund, set is_refund true and make total and gst_hst negative.
- gst_number: only if a GST/HST registration number is printed. Never invent one.
- A forwarded email is still the original document; read the forwarded content.
- Estimates, quotes and order previews are not receipts: set is_receipt false but still fill the fields.
- payment_date: only when a bill states when it will be debited or is due (e.g. "pre-authorized
  debit on Oct 16"); resolve the year from the invoice date.
- summary: plain words for what was bought, e.g. "2 lattes and muffins" or "October fibre internet".
If a field is not on the document, use 0 for amounts and null where the schema allows it."""

CATEGORY_SCHEMA = {
    "type": "object",
    "properties": {
        "account_code": {"type": "string", "description": "One code from the chart of accounts."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "description": "One sentence naming the evidence used."},
    },
    "required": ["account_code", "confidence", "reason"],
}


def category_system(chart: dict) -> str:
    acc = "\n".join(
        f"{a['code']}  {a['name']} [{a['type']}]: {a['description']}"
        + (f" Only for items of ${a['min_amount']} or more." if a.get("min_amount") else "")
        for a in chart["accounts"]
    )
    ent = chart["entity"]
    return f"""You categorise bank and credit-card transactions for {ent['name']}, a small business in
{ent['province']}, Canada. The owner is {ent.get('owner', 'unknown')}.

Chart of accounts:
{acc}

Rules:
- Return exactly one account_code from the list above.
- Use the receipt summary when one is given; it is better evidence than the bank descriptor.
- Money in (positive amount) is income, a transfer, or a refund. A refund goes to the same expense
  account as the original purchase.
- Card payments from the bank and payments received on the card are transfers to 2100, not expenses.
- Payments to the owner are draws (3100). Payments to other people with an invoice are contractor fees.
- Equipment of $500 or more per item is capitalised to 1500; smaller items are office supplies.
- Bank descriptors are truncated and noisy: "SQ *", "TST*", "SP *" and "DASHLY *" are payment
  processors, not the merchant.
- If you cannot tell, return {chart.get('fallback', '9999')} with confidence below 0.5. Do not guess
  with high confidence.
- confidence is your probability that a careful bookkeeper would pick the same account."""


def category_prompt(txn: dict, receipt: dict | None) -> str:
    lines = [f"Date: {txn['date']}", f"Account: {txn['account']}", f"Bank descriptor: {txn['description']}",
             f"Amount (CAD, negative = money out): {txn['amount_cents'] / 100:.2f}"]
    if receipt:
        lines += [f"Matched receipt merchant: {receipt.get('merchant')}",
                  f"Receipt summary: {receipt.get('summary')}",
                  f"Receipt total: {receipt.get('total')} {receipt.get('currency')}"]
    else:
        lines.append("No receipt matched.")
    return "\n".join(lines)


# ---------------------------------------------------------------- interface

class Extractor(Protocol):
    name: str

    def read_receipt(self, doc: ReceiptDoc) -> dict: ...

    def categorise(self, txn: dict, receipt: dict | None) -> dict: ...


# ---------------------------------------------------------------- rules baseline

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
AMT = r"(-?)\s*(?:CDN\$|CA\$|US\$|USD|CAD|\$)?\s*(-?[\d,]+\.\d{2})"
SEP = r"[^\d\n$-]*\n?[^\d\n$-]*"            # label text, optionally a line break (PDF tables)
TOTAL_LABELS = ["total refund", "order total", "total due", "total charges", "amount paid", "total"]
DATE_LABELS = ["refund date", "invoice date", "date paid", "order placed", "departure", "date"]
GST_RE = re.compile(r"\b(\d{5})\s?(\d{4})\s?RT\s?(\d{4})\b")
GENERIC_SENDER = re.compile(r"\b(accounts|billing|receipts?|orders?|noreply|no-reply|team|support)\b", re.I)


def _cents(s: str) -> int:
    return int(round(float(s.replace(",", "")) * 100))


def parse_date(s: str) -> date | None:
    s = s.strip()
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(20\d\d)\b", s)
    if m and m[1][:3].lower() in MONTHS:
        return date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        # Canadian documents usually print DD/MM/YYYY; only unambiguous when one part is > 12.
        day, mon = (a, b) if a > 12 or b <= 12 else (b, a)
        return date(y, mon, day)
    return None


def _labelled_amount(text: str, labels: list[str]) -> int | None:
    for lab in labels:
        found = None
        for m in re.finditer(rf"(?im)^\s*{re.escape(lab)}\b(?!\s*#){SEP}{AMT}", text):
            if lab == "total" and re.search(r"sub\s*total", text[max(0, m.start() - 4):m.end()], re.I):
                continue
            found = m
        if found:
            sign = -1 if found[1] or found[2].startswith("-") else 1
            return sign * abs(_cents(found[2]))
    return None


class RulesExtractor:
    """Regex and keyword baseline. Deterministic; no model calls."""

    name = "rules"

    def __init__(self, chart: dict):
        self.chart = chart
        self.accounts = {a["code"]: a for a in chart["accounts"]}

    # ---- receipts
    def read_receipt(self, doc: ReceiptDoc) -> dict:
        text = doc.text
        low = text.lower()
        is_refund = "refund" in doc.subject.lower() or bool(re.search(r"(?im)^\s*total refund", text))
        is_receipt = not re.search(r"(?im)^\s*(estimate|quote|quotation)\b", text)

        total = _labelled_amount(text, TOTAL_LABELS)
        gst = 0
        m = re.search(rf"(?im)^\s*(?:estimated\s+)?(?:GST/HST|HST|GST)(?!\s*(?:#|reg|registration))"
                      rf"(?:\s*\(?\d+(?:\.\d+)?%\)?)?(?:\s+refunded)?{SEP}{AMT}", text)
        if m:
            gst = abs(_cents(m[2]))
        elif not re.search(r"(?i)\b(PST|QST|RST)\b", text) and not re.search(r"US\$|\bUSD\b", text):
            # A single unlabelled "Tax" line on a Canadian-dollar receipt with no provincial tax
            # shown is read as GST/HST. Wrong for a PST province vendor that lumps taxes together;
            # the Claude extractor is told to separate them.
            m = re.search(rf"(?im)^\s*tax\b{SEP}{AMT}", text)
            if m:
                gst = abs(_cents(m[2]))
        tip = _labelled_amount(text, ["tip", "gratuity"]) or 0
        subtotal = _labelled_amount(text, ["items subtotal", "refund subtotal", "subtotal"])
        if is_refund and total is not None:
            total, gst = -abs(total), -abs(gst)

        currency = "USD" if re.search(r"US\$|\bUSD\b", text) else "CAD"

        when = None
        for lab in DATE_LABELS:
            m = re.search(rf"(?im)^\s*{lab}\s*:?\s*(.+)$", text)
            if m and (when := parse_date(m[1])):
                break
        if when is None:
            m = re.search(r"(?m)^(?:[A-Z][a-z]+day, )?([A-Z][a-z]{2,8}\.? \d{1,2},? 20\d\d)", text)
            when = parse_date(m[1]) if m else None
        if when is None:
            try:
                when = parsedate_to_datetime(doc.sent).date()
            except (TypeError, ValueError):
                when = None

        g = GST_RE.search(text)
        return {
            "is_receipt": is_receipt,
            "merchant": self._merchant(doc),
            "date": when.isoformat() if when else "",
            "currency": currency,
            "subtotal": subtotal / 100 if subtotal is not None else None,
            "gst_hst": gst / 100,
            "other_tax": 0,
            "tip": tip / 100,
            "total": (total or 0) / 100,
            "is_refund": is_refund,
            "gst_number": f"{g[1]}{g[2]}RT{g[3]}" if g else None,
            "invoice_number": None,
            "payment_date": self._payment_date(text, when),
            "summary": self._summary(doc, low),
        }

    @staticmethod
    def _payment_date(text: str, doc_date: date | None) -> str | None:
        m = re.search(r"(?i)(?:debit(?:ed)? on|due date:?|due on|payment date:?)\s+([A-Z][a-z]{2,8}\.? \d{1,2}(?:,? 20\d\d)?)",
                      text)
        if not m or doc_date is None:
            return None
        when = parse_date(m[1]) or parse_date(f"{m[1]}, {doc_date.year}")
        if when and when < doc_date:                   # "due Jan 5" on a December bill
            when = when.replace(year=when.year + 1)
        return when.isoformat() if when else None

    @staticmethod
    def _merchant(doc: ReceiptDoc) -> str:
        for n, t in doc.attachment_texts:              # PDF letterhead: first non-empty line
            first = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
            if first:
                return first
        m = re.search(r"(?im)^Fwd?:\s*.*\bfrom\s+(.+)$", doc.subject) or re.search(
            r"(?i)(?:receipt|folio|refund issued for your|invoice \S+) from (.+)$", doc.subject)
        if m:
            return m[1].strip()
        disp = re.sub(r"\s*<.*>$", "", doc.sender).strip().strip('"')
        disp = re.sub(r"\s+via\s+.*$", "", disp, flags=re.I)
        disp = GENERIC_SENDER.sub("", disp).strip()
        return disp or doc.sender

    @staticmethod
    def _summary(doc: ReceiptDoc, low: str) -> str:
        skip = re.compile(r"(?i)total|subtotal|tax|hst|gst|tip|amount|balance|paid|fee$|refund|charge")
        items = []
        for ln in doc.text.splitlines():
            m = re.match(r"^\s*(?:\d+\s*x\s*)?([A-Za-z][^$\n]{2,70}?)\s*(?:CDN\$|US\$|\$)?\s*[\d,]+\.\d{2}\s*$", ln)
            if m and not skip.search(m[1]):
                items.append(m[1].strip(" .:-"))
        if not items:                                   # PDF tables split label and amount over two lines
            lines = [ln.strip() for ln in doc.text.splitlines()]
            for a, b in zip(lines, lines[1:]):
                if re.fullmatch(r"\$?[\d,]+\.\d{2}", b) and re.match(r"[A-Za-z]", a) and not skip.search(a):
                    items.append(a)
        m = re.search(r"(?im)^item:\s*(.+)$", doc.text)
        if m:
            items.insert(0, m[1])
        return "; ".join(dict.fromkeys(items[:3])) or doc.subject

    # ---- categories
    def categorise(self, txn: dict, receipt: dict | None) -> dict:
        desc = " " + txn["description"].lower() + " "
        rtext = " " + ((receipt or {}).get("summary", "") + " " + (receipt or {}).get("merchant", "")).lower() + " "
        inflow = txn["amount_cents"] > 0
        refund = inflow and ("refund" in desc or bool(receipt and receipt.get("is_refund")))
        owner = (self.chart["entity"].get("owner") or "").lower()
        fallback = self.chart.get("fallback", "9999")

        if owner and owner in desc.lower():
            return {"account_code": "3100", "confidence": 0.9, "reason": "Transfer to the owner named in the entity file."}

        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for code, a in self.accounts.items():
            if inflow and not refund and a["type"] not in {"income", "liability", "equity"}:
                continue
            if (not inflow or refund) and a["type"] == "income":
                continue
            for kw in a.get("keywords", []):
                k = kw.lower()
                pat = rf"(?<![a-z]){re.escape(k)}(?![a-z])"
                if re.search(pat, desc):
                    scores[code] = scores.get(code, 0) + 1.0
                    hits.setdefault(code, []).append(f"descriptor '{kw}'")
                if receipt and re.search(pat, rtext):
                    scores[code] = scores.get(code, 0) + 1.0
                    hits.setdefault(code, []).append(f"receipt '{kw}'")
        amt = abs(txn["amount_cents"])
        for code, a in self.accounts.items():                 # capitalisation threshold
            if code in scores and a.get("min_amount") and amt < a["min_amount"] * 100:
                del scores[code]
        if not scores:
            return {"account_code": fallback, "confidence": 0.2, "reason": "No keyword from the chart of accounts matched."}
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        best, top = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        both = any(h.startswith("descriptor") for h in hits[best]) and any(h.startswith("receipt") for h in hits[best])
        conf = 0.9 if both else 0.75
        if runner >= top:
            conf = 0.45                                         # tie: let a person decide
        elif runner:
            conf -= 0.1
        return {"account_code": best, "confidence": round(conf, 2),
                "reason": f"Matched {', '.join(hits[best][:3])}" + (" (tie with another account)" if runner >= top else "")}


# ---------------------------------------------------------------- Claude

class ClaudeExtractor:
    """Calls Claude through shared.llm.extract with a schema-checked JSON answer."""

    name = "claude"

    def __init__(self, chart: dict, fixtures_dir: Path = FIXTURES):
        self.chart = chart
        self.codes = {a["code"] for a in chart["accounts"]}
        self.fixtures_dir = fixtures_dir
        self._system = category_system(chart)

    def read_receipt(self, doc: ReceiptDoc) -> dict:
        prompt = (f"From: {doc.sender}\nDate sent: {doc.sent}\nSubject: {doc.subject}\n\n"
                  f"--- body ---\n{doc.body}\n")
        for n, t in doc.attachment_texts:
            prompt += f"\n--- attachment: {n} ---\n{t}\n"
        out = llm.extract(module=MODULE, task="receipt", system=RECEIPT_SYSTEM, prompt=prompt,
                          schema=RECEIPT_SCHEMA, fixtures_dir=self.fixtures_dir)
        out.setdefault("subtotal", None)
        out.setdefault("other_tax", 0)
        out.setdefault("tip", 0)
        out.setdefault("invoice_number", None)
        out.setdefault("payment_date", None)
        if out.get("gst_number"):
            g = GST_RE.search(out["gst_number"])
            out["gst_number"] = f"{g[1]}{g[2]}RT{g[3]}" if g else out["gst_number"]
        return out

    def categorise(self, txn: dict, receipt: dict | None) -> dict:
        out = llm.extract(module=MODULE, task="categorise", system=self._system,
                          prompt=category_prompt(txn, receipt), schema=CATEGORY_SCHEMA,
                          fixtures_dir=self.fixtures_dir, max_tokens=512)
        if out.get("account_code") not in self.codes:    # never trust a code that is not in the chart
            out = {"account_code": self.chart.get("fallback", "9999"), "confidence": 0.0,
                   "reason": f"Model returned unknown code {out.get('account_code')!r}."}
        return out


def make_extractor(kind: str | None, chart: dict) -> Extractor:
    if kind is None:
        kind = "claude" if os.environ.get("AFO_LLM_MODE") in {"live", "record"} else "rules"
    return ClaudeExtractor(chart) if kind == "claude" else RulesExtractor(chart)


