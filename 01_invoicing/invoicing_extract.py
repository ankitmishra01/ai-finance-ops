"""Customer-detail extraction: the only step in invoicing that reads unstructured text.

Input: a client's engagement-letter text (new clients only) and that client's emails.
Output: the billing fields below, or None where the documents do not state them.

Two implementations share one interface:
- RulesExtractor: regex and keyword heuristics. Deterministic, runs in tests and CI.
- ClaudeExtractor: calls shared.llm.extract with the JSON schema below.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from shared import llm
from shared.tax import RATES

FIELDS = ["legal_name", "billing_email", "province", "address", "po_number", "split_invoices"]
PROVINCES = sorted(RATES)
PROVINCE_NAMES = {
    "ontario": "ON", "quebec": "QC", "québec": "QC", "british columbia": "BC", "alberta": "AB",
    "manitoba": "MB", "saskatchewan": "SK", "nova scotia": "NS", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "prince edward island": "PE", "yukon": "YT",
    "northwest territories": "NT", "nunavut": "NU",
}
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass
class Email:
    sender: str
    subject: str
    date: str
    body: str


@dataclass
class ExtractionInput:
    client_name: str
    supplier_name: str
    contract_text: str | None = None
    emails: list[Email] = field(default_factory=list)

    def ordered_emails(self) -> list[Email]:
        def key(e: Email):
            try:
                return parsedate_to_datetime(e.date).timestamp()
            except (TypeError, ValueError):
                return 0.0
        return sorted(self.emails, key=key)


class Extractor(Protocol):
    name: str

    def extract(self, inp: ExtractionInput) -> dict: ...


def empty_result() -> dict:
    return {f: None for f in FIELDS} | {"split_invoices": False, "evidence": {}}


# ---------------------------------------------------------------------------
# Rules baseline
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PROV_ALT = "|".join(PROVINCES + [re.escape(n.title()) for n in PROVINCE_NAMES] + list(PROVINCE_NAMES))
POSTAL = r"[A-Z]\d[A-Z] ?\d[A-Z]\d"
# "street, city, PR A1A 1A1" on one line, or "street\ncity, PR A1A 1A1" on two
ADDR_INLINE = re.compile(rf"(\d[^,\n]*?),\s*([A-Z][A-Za-z .'-]+),\s*({PROV_ALT})\s+({POSTAL})")
ADDR_BLOCK = re.compile(rf"^(\d[^\n,]+)\n([A-Z][A-Za-z .'-]+),\s*({PROV_ALT})\s+({POSTAL})", re.M)
# keyword, then up to 40 non-digit characters ("number for this engagement is"), then a token
# containing a digit; "PO-2026-1234" style tokens keep their prefix.
PO_RE = re.compile(
    r"(?:\b(?:P\.O\.|PO)(?=[\s#:]|-\d)|\bpurchase order)[^.\d\n]{0,40}?"
    r"((?:PO-)?(?=[A-Z-]*\d)[A-Z0-9][A-Z0-9-]{3,}\d)", re.I)
BILLING_CUE = re.compile(r"invoice|billing|payable|\bAP\b", re.I)
LEGAL_NAME_EMAIL = re.compile(
    r"legal name(?: is|,|:)?\s+([A-Z][\w&.' -]*?(?:Inc\.|Ltd\.|Corp\.|Limited|LLP))", re.I)
LEGAL_NAME_CONTRACT = re.compile(r'and ([A-Z][^"\n]*?) \("Client"\)')


def _province_code(token: str) -> str | None:
    t = token.strip()
    if t.upper() in RATES:
        return t.upper()
    return PROVINCE_NAMES.get(t.lower())


def _fresh_text(body: str) -> str:
    """Drop quoted lines (older messages) so the newest instruction wins."""
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(">"))


class RulesExtractor:
    name = "rules"

    def extract(self, inp: ExtractionInput) -> dict:
        out = empty_result()
        ev = out["evidence"]
        emails = inp.ordered_emails()
        # Newest message first; quoted history is ignored.
        texts = [(_fresh_text(e.body), e) for e in reversed(emails)]

        # legal name: an explicit "legal name" instruction in email beats the contract
        for t, _ in texts:
            m = LEGAL_NAME_EMAIL.search(t)
            if m:
                out["legal_name"] = m.group(1).strip()
                ev["legal_name"] = m.group(0)
                break
        if not out["legal_name"] and inp.contract_text:
            m = LEGAL_NAME_CONTRACT.search(inp.contract_text)
            if m:
                out["legal_name"] = m.group(1).strip()
                ev["legal_name"] = m.group(0)

        # billing email: first address in a sentence that mentions invoices/billing/AP
        for t, e in texts:
            for sentence in re.split(r"(?<=[.!?])\s+|\n", t):
                if not BILLING_CUE.search(sentence):
                    continue
                # In "send to X instead of Y" the first address is the new one.
                addrs = EMAIL_RE.findall(sentence)
                if addrs:
                    out["billing_email"] = addrs[0].rstrip(".").lower()
                    ev["billing_email"] = sentence.strip()
                    break
                if re.search(r"invoice me|bill me", sentence, re.I):
                    out["billing_email"] = parseaddr(e.sender)[1].lower()
                    ev["billing_email"] = sentence.strip()
                    break
            if out["billing_email"]:
                break

        # address: an address in an email (a bill-to or move notice) beats the contract's
        found = None
        for t, _ in texts:
            m = ADDR_BLOCK.search(t) or ADDR_INLINE.search(t)
            if m:
                found = m
                break
        if not found and inp.contract_text:
            found = ADDR_INLINE.search(inp.contract_text)
        if found:
            prov = _province_code(found.group(3))
            street, city, postal = found.group(1).strip(), found.group(2).strip(), found.group(4)
            out["province"] = prov
            out["address"] = f"{street}, {city}, {prov} {postal}"
            ev["address"] = found.group(0)

        # PO number
        for t, _ in texts + [(inp.contract_text or "", None)]:
            m = PO_RE.search(t)
            if m:
                out["po_number"] = m.group(1)
                ev["po_number"] = m.group(0)
                break

        # split request
        for t, _ in texts:
            if re.search(r"\bsplit\b[^.]*\binvoice|\binvoice[^.]*\bsplit\b|two (separate )?invoices", t, re.I):
                out["split_invoices"] = True
                ev["split_invoices"] = "split"
                break
        return out


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": FIELDS + ["evidence"],
    "properties": {
        "legal_name": {
            "type": ["string", "null"],
            "description": "Client's legal entity name as it must appear on invoices. Null if not stated.",
        },
        "billing_email": {
            "type": ["string", "null"],
            "description": "Address invoices must be sent to, exactly as written. Null if not stated.",
        },
        "province": {
            "type": ["string", "null"],
            "enum": PROVINCES + [None],
            "description": "Two-letter code of the province or territory of the bill-to address.",
        },
        "address": {
            "type": ["string", "null"],
            "description": "Bill-to postal address formatted 'street, city, PR A1A 1A1' with a two-letter code.",
        },
        "po_number": {
            "type": ["string", "null"],
            "description": "Purchase order number exactly as written, without words like 'PO' or '#'.",
        },
        "split_invoices": {
            "type": "boolean",
            "description": "True only if the client asks for this period's billing to be split across invoices.",
        },
        "evidence": {
            "type": "object",
            "description": "For each non-null field, a short verbatim quote from the documents supporting it.",
            "additionalProperties": {"type": "string"},
        },
    },
}

SYSTEM = """You support the accounts-receivable team of a Canadian professional-services firm.
You read a client's engagement letter and the client's emails and return the billing details the
firm needs to issue a GST/HST invoice. A person reviews every result before it is used.

Rules:
- Report only what the documents state. Never infer, complete or invent an email address,
  PO number, name or address. If a field is not stated, return null.
- The supplier (the firm issuing invoices) is named in the request. Never return the supplier's
  own name, address or email as the client's.
- Emails are given oldest first. Lines starting with ">" are quoted older text. When a later
  message corrects or replaces an earlier instruction, use the later one.
- legal_name: the client's legal entity name for invoices. If the client says invoices must show a
  different name from the one in the letter (for example a legal name instead of an operating name),
  use the name the client asks for. Keep the suffix (Inc., Ltd., Corp., Limited) as written.
- billing_email: the address invoices should go to. A sender's own address counts only if the sender
  says to invoice them directly. An address the client says is old or no longer used is not the answer.
  A promise to send the contact later means null.
- address and province: the bill-to address. If the client names a different billing office or a new
  address, use that instead of the address in the letter. Write the province as its two-letter code
  (ON, QC, BC, AB, MB, SK, NS, NB, NL, PE, YT, NT, NU) in both fields, and the postal code as "A1A 1A1".
- po_number: the purchase order to quote on invoices. "We don't use purchase orders" means null. If the
  client replaces an old PO with a new one, return the new one.
- split_invoices: true only when the client asks for the billing to be divided across more than one
  invoice. Otherwise false.
- If the request is a change notice from an existing customer and no engagement letter is given,
  fill only the fields the email sets or changes; leave every other field null.
- evidence: for every non-null field, a verbatim quote of at most 25 words."""


def build_prompt(inp: ExtractionInput) -> str:
    parts = [f"Supplier (the firm issuing invoices): {inp.supplier_name}",
             f"Client as named in our ERP: {inp.client_name}", ""]
    if inp.contract_text:
        parts += ["<engagement_letter>", inp.contract_text.strip(), "</engagement_letter>", ""]
    else:
        parts += ["No engagement letter: this client already has a customer record. Return only changes.", ""]
    for i, e in enumerate(inp.ordered_emails(), 1):
        parts += [f'<email index="{i}" from="{e.sender}" date="{e.date}" subject="{e.subject}">',
                  e.body.strip(), "</email>", ""]
    parts.append("Return the billing details with the submit tool.")
    return "\n".join(parts)


class ClaudeExtractor:
    name = "claude"

    def __init__(self, fixtures_dir: Path = FIXTURES) -> None:
        self.fixtures_dir = fixtures_dir

    def extract(self, inp: ExtractionInput) -> dict:
        out = llm.extract(module="invoicing", task="customer_details", system=SYSTEM,
                          prompt=build_prompt(inp), schema=SCHEMA, fixtures_dir=self.fixtures_dir)
        res = empty_result()
        res.update({k: out.get(k) for k in FIELDS if k in out})
        res["split_invoices"] = bool(out.get("split_invoices"))
        res["evidence"] = out.get("evidence") or {}
        if res["province"] not in RATES:
            res["province"] = None
        return res


def get_extractor(name: str | None = None) -> Extractor:
    """Explicit name wins; otherwise Claude in live/record mode, rules in replay (the CI default)."""
    if name is None:
        name = "claude" if os.environ.get("AFO_LLM_MODE", "replay") in {"live", "record"} else "rules"
    if name == "claude":
        return ClaudeExtractor()
    if name == "rules":
        return RulesExtractor()
    raise ValueError(f"unknown extractor {name!r}")


# ---------------------------------------------------------------------------
# Scoring helpers (used by the eval and tests)
# ---------------------------------------------------------------------------

def normalize(fieldname: str, value):
    if fieldname == "split_invoices":
        return bool(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    v = str(value).strip()
    if fieldname == "billing_email":
        return v.lower().rstrip(".")
    if fieldname == "province":
        return _province_code(v) or v.upper()
    if fieldname == "po_number":
        v = re.sub(r"^(?:P\.?O\.?|purchase order)\s*(?:number|no\.?)?[\s#:-]*", "", v, flags=re.I)
        return re.sub(r"\s+", "", v).upper()
    if fieldname == "address":
        v = v.lower()
        for name, code in PROVINCE_NAMES.items():
            v = re.sub(rf"\b{name}\b", code.lower(), v)
        v = re.sub(r"[^a-z0-9]+", " ", v)
        return re.sub(r"\b([a-z]\d[a-z]) (\d[a-z]\d)\b", r"\1\2", v).strip()
    if fieldname == "legal_name":
        return re.sub(r"\s+", " ", v).casefold().rstrip(" .,")
    return v
