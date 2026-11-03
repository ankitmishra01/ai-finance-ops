"""What a reviewer sees next to each proposal: the source document and the figures behind it.

Read-only. Everything here comes from the stored queue (shared/approvals.py), the files the
run.py scripts wrote under out/, and the synthetic inputs under data/. Used by the web UI and by
scripts/build_site.py, so the local review screen and the static demo show the same evidence.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from functools import lru_cache
from pathlib import Path

import yaml

from shared.approvals import ApprovalQueue, Proposal

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic"


@dataclass(frozen=True)
class Module:
    id: str
    slug: str
    title: str
    job: str
    approves: str

    @property
    def readme(self) -> Path:
        return ROOT / self.id / "README.md"

    def intro(self) -> str:
        """First paragraph of the module README (after its title)."""
        if not self.readme.exists():
            return ""
        paras = [p.strip() for p in self.readme.read_text().split("\n\n")]
        return next((p for p in paras if p and not p.startswith(("#", "```", "|", "-"))), "")


MODULES = [
    Module("01_invoicing", "invoicing", "Invoicing", "Month-end client invoicing",
           "New customers, customer changes, each invoice"),
    Module("02_bills", "bills", "Bills", "Accounts payable intake",
           "Each bill before it is posted"),
    Module("03_payroll", "payroll", "Payroll", "Ontario pay run",
           "Each pay line, then the run"),
    Module("04_gst_hst", "gst_hst", "GST/HST", "Quarterly GST/HST return",
           "Flagged transactions, then the return"),
    Module("05_bookkeeping", "bookkeeping", "Bookkeeping", "Records and evidence",
           "Each evidence link and category change"),
]
BY_ID = {m.id: m for m in MODULES}

KIND_LABELS = {
    "customer": "New customer",
    "customer_update": "Customer change",
    "invoice": "Invoice",
    "bill": "Bill",
    "pay_line": "Pay line",
    "gst_hst_txn_review": "GST/HST transaction",
    "gst_hst_return": "GST34 return",
    "evidence_link": "Evidence link",
    "recategorise": "Category change",
}


# ---------------------------------------------------------------- formatting

def cents(v) -> str:
    if v is None or v == "":
        return "-"
    n = int(v)
    return f"-${abs(n) / 100:,.2f}" if n < 0 else f"${n / 100:,.2f}"


def dollars(v) -> str:
    if v is None or v == "":
        return "-"
    d = Decimal(str(v))
    return f"-${abs(d):,.2f}" if d < 0 else f"${d:,.2f}"


def humanise(key: str) -> str:
    return key.replace("_cents", "").replace("_", " ").strip().capitalize()


# ---------------------------------------------------------------- evidence

@lru_cache(maxsize=1)
def gst_line_labels() -> dict[str, str]:
    rules = yaml.safe_load((ROOT / "04_gst_hst" / "rules.yaml").read_text())
    return {str(k): str(v) for k, v in (rules.get("return_lines") or {}).items()}


def _csv_row(path: Path, key: str, value: str) -> dict | None:
    if not path.exists():
        return None
    with path.open(newline="") as f:
        return next((r for r in csv.DictReader(f) if r.get(key) == value), None)


def _read_eml(path: Path) -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    body = msg.get_body(preferencelist=("plain", "html"))
    pdfs = [a for a in msg.iter_attachments() if a.get_content_type() == "application/pdf"]
    return {
        "from": str(msg["From"] or ""), "subject": str(msg["Subject"] or ""), "date": str(msg["Date"] or ""),
        "body": body.get_content() if body else "",
        "attachments": [a.get_filename() or "attachment" for a in msg.iter_attachments()],
        "pdf": pdfs[0].get_payload(decode=True) if pdfs else None,
    }


def source_path(module: str, p: Proposal, out: Path) -> Path | None:
    """The document behind a proposal: a PDF, or an .eml whose body or PDF attachment is the evidence."""
    pl = p.payload
    if p.kind == "invoice":
        for c in (out / "invoicing" / "invoices" / f"{pl.get('number')}.pdf",
                  out / "invoicing" / "drafts" / f"{pl.get('number')}-DRAFT.pdf"):
            if c.exists():
                return c
    elif p.kind == "bill" and pl.get("source"):
        c = DATA / "bills" / "inbox" / Path(pl["source"]).name
        return c if c.exists() else None
    elif p.kind == "evidence_link" and pl.get("evidence_path"):
        base = (out / "bookkeeping").resolve()
        c = (base / pl["evidence_path"]).resolve()
        return c if c.is_relative_to(base) and c.exists() else None
    elif p.kind in {"customer", "customer_update"}:
        return None
    return None


def source_pdf(module: str, p: Proposal, out: Path) -> bytes | None:
    path = source_path(module, p, out)
    if path is None:
        return None
    if path.suffix == ".pdf":
        return path.read_bytes()
    if path.suffix == ".eml":
        return _read_eml(path)["pdf"]
    return None


def build(module: str, index: int, p: Proposal, q: ApprovalQueue, out: Path) -> dict:
    """Template context for one proposal's evidence panel."""
    pl = p.payload
    ev: dict = {"kind": p.kind, "label": KIND_LABELS.get(p.kind, p.kind), "payload": pl, "rows": [],
                "tables": [], "quotes": {}, "email": None, "pdf": False, "source_name": None}
    src = source_path(module, p, out)
    if src is not None:
        ev["source_name"] = src.name
        if src.suffix == ".eml":
            mail = _read_eml(src)
            ev["email"] = {k: v for k, v in mail.items() if k != "pdf"}
            ev["pdf"] = mail["pdf"] is not None
        else:
            ev["pdf"] = src.suffix == ".pdf"

    if p.kind in {"customer", "customer_update"}:
        ev["rows"] = [(humanise(k), pl.get(k) or "-") for k in
                      ("client_id", "legal_name", "billing_email", "province", "address", "po_number")]
        if pl.get("changes"):
            ev["tables"].append({"title": "Changes", "head": ["Field", "From", "To"],
                                 "rows": [[humanise(k), v.get("from") or "-", v.get("to") or "-"]
                                          for k, v in pl["changes"].items()]})
        ev["quotes"] = pl.get("evidence") or {}

    elif p.kind == "invoice":
        cust = pl.get("customer") or {}
        ev["rows"] = [("Number", pl.get("number")), ("Customer", cust.get("legal_name")),
                      ("Billing email", cust.get("billing_email")), ("PO", cust.get("po_number") or "-"),
                      ("Period", pl.get("period")), ("Issue date", pl.get("issue_date")),
                      ("Due date", pl.get("due_date")), ("Terms", pl.get("payment_terms")),
                      ("Place of supply", pl.get("place_of_supply")),
                      ("Tax", pl.get("tax_label") or pl.get("tax_code"))]
        lines = [[l.get("description"), l.get("quantity") or "", cents(l.get("unit_price_cents")),
                  cents(l.get("amount_cents"))] for l in pl.get("lines", [])]
        lines += [["Subtotal", "", "", cents(pl.get("subtotal_cents"))],
                  [pl.get("tax_label") or "Tax", "", "", cents(pl.get("tax_cents"))],
                  ["Total", "", "", cents(pl.get("total_cents"))]]
        ev["tables"].append({"title": "Invoice lines", "head": ["Description", "Qty", "Rate", "Amount"],
                             "rows": lines, "num": [2, 3], "foot": 3})

    elif p.kind == "bill":
        ev["rows"] = [("Vendor", pl.get("vendor")), ("Invoice number", pl.get("number")),
                      ("GST/HST number", pl.get("gst_number") or "-"), ("Bill date", pl.get("bill_date")),
                      ("Due date", pl.get("due_date")), ("Terms", pl.get("terms") or "-"),
                      ("Currency", pl.get("currency")), ("Subtotal", cents(pl.get("subtotal_cents"))),
                      ("GST/HST", cents(pl.get("tax_cents"))), ("Other tax", cents(pl.get("other_tax_cents"))),
                      ("Total", cents(pl.get("total_cents"))), ("ITC status", pl.get("itc_status")),
                      ("ITC claimable", cents(pl.get("itc_claimable_cents"))),
                      ("Category", f"{pl.get('category')} ({pl.get('account')})"), ("Due bucket", pl.get("bucket")),
                      ("Source", pl.get("source"))]

    elif p.kind == "pay_line":
        ev["rows"] = [("Employee", f"{pl.get('name')} ({pl.get('employee_id')})")]
        ev["tables"].append({"title": "Earnings", "head": ["Type", "Amount"],
                             "rows": [[e[0], dollars(e[1])] for e in pl.get("earnings", [])], "num": [1]})
        ded = [("Gross", "gross"), ("CPP", "cpp"), ("CPP2", "cpp2"), ("EI", "ei"),
               ("Federal tax", "federal_tax"), ("Ontario tax", "ontario_tax"), ("Net pay", "net")]
        ev["tables"].append({"title": "Deductions (employee)", "head": ["", "Amount"],
                             "rows": [[n, dollars(pl.get(k))] for n, k in ded], "num": [1], "foot": 1})
        ev["tables"].append({"title": "Employer contributions", "head": ["", "Amount"],
                             "rows": [[n, dollars(pl.get(k))] for n, k in
                                      [("CPP", "employer_cpp"), ("CPP2", "employer_cpp2"), ("EI", "employer_ei")]],
                             "num": [1]})
        ev["quotes"] = {f"{a.get('type')} ({a.get('source')})": a.get("evidence")
                        for a in pl.get("adjustments", []) if a.get("evidence")}
        ev["notes"] = pl.get("notes", [])
        ev["run_summary"] = payroll_summary(q)

    elif p.kind == "gst_hst_txn_review":
        txn = _csv_row(DATA / "gst_hst" / "transactions.csv", "txn_id", pl.get("txn_id", "")) or {}
        ev["rows"] = [("Transaction", pl.get("txn_id")), ("Date", txn.get("date", "-")),
                      ("Description", txn.get("description", "-")), ("Amount", dollars(txn.get("amount"))),
                      ("GST/HST on the line", dollars(txn.get("gst_hst"))),
                      ("Supplier BN", txn.get("supplier_bn") or "-"), ("Category", pl.get("category")),
                      ("Supply type", pl.get("supply_type")), ("ITC treatment", pl.get("itc_treatment")),
                      ("Place of supply", pl.get("place_of_supply")),
                      ("Classifier confidence", pl.get("confidence")),
                      ("Held out of the return", "yes" if pl.get("held_out") else "no")]
        labels = gst_line_labels()
        ev["tables"].append({"title": "GST34 lines this transaction feeds", "head": ["Line", "Amount", "What it is"],
                             "rows": [[k, cents(v), labels.get(k, "")] for k, v in (pl.get("lines") or {}).items()],
                             "num": [1]})

    elif p.kind == "gst_hst_return":
        ev["rows"] = [("Due date", pl.get("due_date")), ("On time until", pl.get("effective_due_date"))]
        ev["tables"].append({"title": "GST34 lines", "head": ["Line", "Amount", "What it is"],
                             "rows": gst34_rows(pl.get("lines_cents") or {}), "num": [1]})
        ev["package"] = pl.get("package") or {}

    elif p.kind == "evidence_link":
        ev["rows"] = [("Receipt", pl.get("receipt_id")), ("Transactions", ", ".join(pl.get("txn_ids", []))),
                      ("Match", pl.get("kind")), ("Description similarity", pl.get("similarity")),
                      ("Amount difference", cents(pl.get("amount_diff_cents"))),
                      ("Days apart", pl.get("days_apart")), ("Filed as", pl.get("evidence_path"))]
        txns = [_csv_row(DATA / "bookkeeping" / "transactions.csv", "txn_id", t) for t in pl.get("txn_ids", [])]
        ev["tables"].append({"title": "Bank and card lines", "head": ["Txn", "Date", "Account", "Description", "Amount"],
                             "rows": [[t["txn_id"], t["date"], t["account"], t["description"], dollars(t["amount"])]
                                      for t in txns if t], "num": [4]})

    elif p.kind == "recategorise":
        txn = _csv_row(DATA / "bookkeeping" / "transactions.csv", "txn_id", pl.get("txn_id", "")) or {}
        ev["rows"] = [("Transaction", pl.get("txn_id")), ("Date", txn.get("date", "-")),
                      ("Description", txn.get("description", "-")), ("Amount", dollars(txn.get("amount"))),
                      ("From account", pl.get("from")), ("To account", pl.get("to")),
                      ("Confidence", pl.get("confidence")), ("Why", pl.get("reason"))]
    return ev


def gst34_rows(lines: dict) -> list[list]:
    labels = gst_line_labels()
    order = list(labels) or list(lines)
    return [[k, cents(lines.get(k, 0)), _short(labels.get(k, ""))] for k in order if k in lines]


def _short(label: str) -> str:
    return re.split(r"[;.(]", label, maxsplit=1)[0].strip()


def payroll_summary(q: ApprovalQueue) -> dict:
    """Totals for the whole run, from every pay line in the queue."""
    keys = ["gross", "cpp", "cpp2", "ei", "federal_tax", "ontario_tax", "net",
            "employer_cpp", "employer_cpp2", "employer_ei"]
    tot = {k: Decimal("0") for k in keys}
    lines = [p for p in q.items if p.kind == "pay_line"]
    for p in lines:
        for k in keys:
            tot[k] += Decimal(str(p.payload.get(k) or "0"))
    remit = sum(tot[k] for k in ("cpp", "cpp2", "ei", "federal_tax", "ontario_tax",
                                 "employer_cpp", "employer_cpp2", "employer_ei"))
    return {"employees": len(lines), "flagged": sum(bool(p.flags) for p in lines),
            "rows": [["Gross pay", dollars(tot["gross"])], ["CPP + CPP2", dollars(tot["cpp"] + tot["cpp2"])],
                     ["EI", dollars(tot["ei"])], ["Income tax", dollars(tot["federal_tax"] + tot["ontario_tax"])],
                     ["Net pay", dollars(tot["net"])], ["PD7A remittance if all approved", dollars(remit)]]}
