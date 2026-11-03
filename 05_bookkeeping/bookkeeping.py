"""Month-end bookkeeping agent: make every bank and card line audit-ready.

Pipeline (see README.md for the diagram):
  read receipts (Extractor) -> match to transactions (deterministic) -> propose categories
  (Extractor) -> stage evidence files -> exceptions report -> ApprovalQueue -> ledger.

Nothing is written to the ledger here except through apply_approved(), which only acts
on proposals a person (or --auto-approve, which holds anything flagged) approved.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from shared import ledger  # noqa: E402
from shared.approvals import ApprovalQueue  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

from bookkeeping_extract import LOW_CONFIDENCE, Extractor, ReceiptDoc, doc_from_mail  # noqa: E402
from bookkeeping_match import Link, MatchResult, Receipt, Txn, match  # noqa: E402

DATA = ROOT / "data" / "synthetic" / "bookkeeping"
OUT = ROOT / "out" / "bookkeeping"
ITC_BN_THRESHOLD_CENTS = 10_000      # SOR/91-45 s.3(b): registration number needed from $100


# ---------------------------------------------------------------- inputs

def load_chart(data_dir: Path = DATA) -> dict:
    return yaml.safe_load((data_dir / "chart_of_accounts.yaml").read_text())


def load_transactions(data_dir: Path = DATA) -> list[dict]:
    current = {}
    with (data_dir / "ledger_current.csv").open() as f:
        for row in csv.DictReader(f):
            current[row["txn_id"]] = row["category"]
    out = []
    with (data_dir / "transactions.csv").open() as f:
        for row in csv.DictReader(f):
            out.append({"txn_id": row["txn_id"], "date": row["date"], "account": row["account"],
                        "description": row["description"], "amount_cents": parse_cents(row["amount"]),
                        "category": current.get(row["txn_id"], "9999")})
    return out


def parse_cents(s: str) -> int:
    s = s.strip().replace(",", "").replace("$", "")
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    whole, _, frac = s.strip("-()").partition(".")
    c = int(whole or 0) * 100 + int((frac + "00")[:2])
    return -c if neg else c


def load_docs(data_dir: Path = DATA) -> list[ReceiptDoc]:
    return [doc_from_mail(m) for m in read_mailbox(data_dir / "mailbox")]


# ---------------------------------------------------------------- helpers

def to_cents(x) -> int:
    return int(round(float(x or 0) * 100))


def receipt_from_fields(rid: str, f: dict) -> Receipt:
    try:
        d = date.fromisoformat(f.get("date") or "")
    except ValueError:
        d = date(1900, 1, 1)                    # unreadable date: will not match, shows as orphan
    total = to_cents(f.get("total"))
    if f.get("is_refund") and total > 0:
        total = -total
    try:
        pay = date.fromisoformat(f["payment_date"]) if f.get("payment_date") else None
    except ValueError:
        pay = None
    return Receipt(rid, f.get("merchant") or "", d, total, (f.get("currency") or "CAD").upper(),
                   to_cents(f.get("gst_hst")), f.get("gst_number"), bool(f.get("is_receipt", True)),
                   f.get("invoice_number"), f.get("summary") or "", pay)


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "unknown"


def evidence_path(r: Receipt, ext: str) -> Path:
    """evidence/<YYYY-MM>/<date>_<merchant>_<amount>.<ext>, relative to the output dir."""
    amt = f"{r.total_cents / 100:.2f}"
    if r.currency != "CAD":
        amt += r.currency
    return Path("evidence") / r.date.strftime("%Y-%m") / f"{r.date.isoformat()}_{slug(r.merchant)}_{amt}{ext}"


def money(c: int) -> str:
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


def receipt_flags(r: Receipt) -> list[str]:
    flags = []
    if r.currency == "CAD" and r.gst_hst_cents != 0 and abs(r.total_cents) >= ITC_BN_THRESHOLD_CENTS and not r.gst_number:
        flags.append("missing GST/HST registration number on a $100+ purchase (needed to claim the ITC)")
    return flags


# ---------------------------------------------------------------- the run

@dataclass
class RunResult:
    receipts: dict[str, dict]                    # receipt_id -> extracted fields
    matches: MatchResult
    categories: dict[str, dict]                  # txn_id -> {account_code, confidence, reason}
    txns: list[dict]
    queue: ApprovalQueue
    exceptions: dict[str, list[dict]] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)        # receipt_id -> staged path


def run(extractor: Extractor, data_dir: Path = DATA, out_dir: Path = OUT, chart: dict | None = None) -> RunResult:
    chart = chart or load_chart(data_dir)
    accounts = {a["code"]: a for a in chart["accounts"]}
    txns = load_transactions(data_dir)
    docs = {d.receipt_id: d for d in load_docs(data_dir)}

    # 1. Read every receipt.
    fields = {rid: extractor.read_receipt(doc) for rid, doc in docs.items()}
    receipts = {rid: receipt_from_fields(rid, f) for rid, f in fields.items()}

    # 2. Match.
    tx = [Txn(t["txn_id"], date.fromisoformat(t["date"]), t["account"], t["description"], t["amount_cents"])
          for t in txns]
    m = match(list(receipts.values()), tx)
    link_of_txn: dict[str, Link] = {tid: l for l in m.links for tid in l.txn_ids}
    dup_charge = {b: a for a, b in m.duplicate_charges}

    # 3. Categorise every transaction, using the matched receipt where there is one.
    cats = {}
    for t in txns:
        link = link_of_txn.get(t["txn_id"])
        cats[t["txn_id"]] = extractor.categorise(t, fields[link.receipt_id] if link else None)

    # 4. Stage evidence files.
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str] = {}
    for link in m.links:
        r, doc = receipts[link.receipt_id], docs[link.receipt_id]
        rel = evidence_path(r, Path(doc.evidence_name).suffix or ".eml")
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        while dest.exists() and dest.read_bytes() != doc.evidence_bytes:   # same name, different document
            dest = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
            n += 1
        dest.write_bytes(doc.evidence_bytes)
        staged[link.receipt_id] = str(dest.relative_to(out_dir))

    # 5. Proposals.
    q = ApprovalQueue()
    by_id = {t["txn_id"]: t for t in txns}
    for link in m.links:
        r = receipts[link.receipt_id]
        # An FX conversion inside the rate band is noted in the summary; it is not a reason to hold.
        flags = [f for f in link.flags if not f.startswith("FX ")] + receipt_flags(r)
        for tid in link.txn_ids:
            if tid in dup_charge:
                flags.append(f"possible duplicate charge of {dup_charge[tid]}")
        note = f" [{link.flags[0]}]" if link.kind == "fx" else ""
        tlist = ", ".join(f"{tid} {by_id[tid]['description']} {money(by_id[tid]['amount_cents'])}" for tid in link.txn_ids)
        q.propose("evidence_link",
                  f"{link.receipt_id} {r.merchant} {r.date} {money(r.total_cents)}{' ' + r.currency if r.currency != 'CAD' else ''}"
                  f" -> {tlist} ({link.kind}, sim {link.similarity:.2f}){note}",
                  {"receipt_id": link.receipt_id, "txn_ids": link.txn_ids, "evidence_path": staged[link.receipt_id],
                   "kind": link.kind, "similarity": link.similarity, "amount_diff_cents": link.amount_diff_cents,
                   "days_apart": link.days_apart},
                  flags)
    for t in txns:
        c = cats[t["txn_id"]]
        # No proposal when nothing would change, or when the extractor could only offer the
        # suspense account: moving a categorised line into suspense helps nobody. Those lines
        # appear in the low-confidence section of the exceptions report instead.
        if c["account_code"] in (t["category"], chart.get("fallback", "9999")):
            continue
        flags = []
        if c["confidence"] < LOW_CONFIDENCE:
            flags.append(f"low confidence {c['confidence']:.2f}")
        q.propose("recategorise",
                  f"{t['txn_id']} {t['description']} {money(t['amount_cents'])}: "
                  f"{t['category']} {accounts.get(t['category'], {}).get('name', '?')} -> "
                  f"{c['account_code']} {accounts[c['account_code']]['name']} ({c['confidence']:.2f}: {c['reason']})",
                  {"txn_id": t["txn_id"], "from": t["category"], "to": c["account_code"],
                   "confidence": c["confidence"], "reason": c["reason"]},
                  flags)

    res = RunResult(fields, m, cats, txns, q, evidence=staged)
    res.exceptions = build_exceptions(res, receipts, accounts, chart)
    return res


def build_exceptions(res: RunResult, receipts: dict[str, Receipt], accounts: dict, chart: dict) -> dict[str, list[dict]]:
    by_id = {t["txn_id"]: t for t in res.txns}
    m = res.matches
    ex: dict[str, list[dict]] = {k: [] for k in
                                 ["no_evidence", "statement_only", "orphan_receipts", "duplicate_receipts",
                                  "not_receipts", "duplicate_charges", "amount_mismatches", "low_confidence",
                                  "missing_gst_number"]}
    for tid in m.unmatched_txns:
        t = by_id[tid]
        c = res.categories[tid]
        code = c["account_code"] if c["confidence"] >= LOW_CONFIDENCE else t["category"]
        needs = accounts.get(code, {}).get("evidence_required", True)
        row = {"txn_id": tid, "date": t["date"], "description": t["description"],
               "amount": money(t["amount_cents"]), "category": code}
        ex["no_evidence" if needs else "statement_only"].append(row)
    for rid in m.orphan_receipts:
        r = receipts[rid]
        ex["orphan_receipts"].append({"receipt_id": rid, "merchant": r.merchant, "date": str(r.date),
                                      "total": money(r.total_cents) + ("" if r.currency == "CAD" else " " + r.currency),
                                      "note": "total not read; check the document" if r.total_cents == 0 else
                                      "no bank or card line within the date window"})
    for dup, orig in m.duplicate_receipts.items():
        ex["duplicate_receipts"].append({"receipt_id": dup, "duplicate_of": orig, "merchant": receipts[dup].merchant})
    for rid in m.not_receipts:
        r = receipts[rid]
        ex["not_receipts"].append({"receipt_id": rid, "merchant": r.merchant, "total": money(r.total_cents),
                                   "note": "quote or estimate, not evidence of payment"})
    for a, b in m.duplicate_charges:
        ex["duplicate_charges"].append({"txn_id": b, "same_as": a, "description": by_id[b]["description"],
                                        "amount": money(by_id[b]["amount_cents"])})
    for l in m.links:
        if l.kind == "mismatch":
            ex["amount_mismatches"].append({"receipt_id": l.receipt_id, "txn_id": l.txn_ids[0], "detail": l.flags[0]})
        for f in receipt_flags(receipts[l.receipt_id]):
            ex["missing_gst_number"].append({"receipt_id": l.receipt_id, "merchant": receipts[l.receipt_id].merchant,
                                             "total": money(receipts[l.receipt_id].total_cents), "detail": f})
    for tid, c in res.categories.items():
        if c["confidence"] < LOW_CONFIDENCE:
            ex["low_confidence"].append({"txn_id": tid, "description": by_id[tid]["description"],
                                         "proposed": c["account_code"], "current": by_id[tid]["category"],
                                         "confidence": c["confidence"], "reason": c["reason"]})
    return ex


# ---------------------------------------------------------------- outputs

EX_TITLES = {
    "no_evidence": "Transactions with no evidence",
    "statement_only": "No receipt, statement is the record (attach statement / payout report)",
    "orphan_receipts": "Receipts with no matching transaction",
    "duplicate_receipts": "Duplicate receipts (not linked)",
    "not_receipts": "Documents that are not receipts",
    "duplicate_charges": "Possible duplicate charges",
    "amount_mismatches": "Amount mismatches",
    "low_confidence": f"Low-confidence categories (< {LOW_CONFIDENCE})",
    "missing_gst_number": "Missing GST/HST registration number",
}


def write_outputs(res: RunResult, out_dir: Path = OUT, period: str = "") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    res.queue.write(out_dir / "proposals.json")
    (out_dir / "receipts_extracted.json").write_text(json.dumps(res.receipts, indent=2, default=str))
    (out_dir / "matches.json").write_text(json.dumps([asdict(l) for l in res.matches.links], indent=2))
    lines = [f"# Bookkeeping exceptions {period}".rstrip(), ""]
    with (out_dir / "exceptions.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "detail"])
        for key, rows in res.exceptions.items():
            lines += [f"## {EX_TITLES[key]} ({len(rows)})", ""]
            if not rows:
                lines += ["None.", ""]
                continue
            cols = list(rows[0])
            lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for r in rows:
                lines.append("| " + " | ".join(str(r[c]).replace("|", "/") for c in cols) + " |")
                w.writerow([key, json.dumps(r)])
            lines.append("")
    (out_dir / "exceptions.md").write_text("\n".join(lines))


def review_table(q: ApprovalQueue, width: int = 150) -> str:
    rows = [f"{'#':>3}  {'kind':<14} {'decision':<9} summary / flags", "-" * width]
    for i, p in enumerate(q.items, 1):
        rows.append(f"{i:>3}  {p.kind:<14} {p.decision:<9} {p.summary[:width - 30]}")
        for f in p.flags:
            rows.append(f"{'':>28}FLAG: {f}")
    return "\n".join(rows)


# ---------------------------------------------------------------- ledger

def load_ledger(con, txns: list[dict]) -> dict[str, int]:
    """Insert the month's transactions into the ledger; returns txn_id -> ledger row id."""
    ids = {}
    for t in txns:
        ids[t["txn_id"]] = ledger.add_transaction(con, date=t["date"], description=t["description"],
                                                  amount_cents=t["amount_cents"], account=t["account"],
                                                  category=t["category"])
    con.commit()
    return ids


def apply_approved(con, q: ApprovalQueue, ids: dict[str, int], out_dir: Path = OUT) -> dict[str, int]:
    done = {"evidence_links": 0, "recategorised": 0}
    for p in q.items:
        if p.decision != "approved":
            continue
        if p.kind == "evidence_link":
            for tid in p.payload["txn_ids"]:
                ledger.update_transaction(con, ids[tid], evidence_path=str(out_dir / p.payload["evidence_path"]))
                done["evidence_links"] += 1
        elif p.kind == "recategorise":
            ledger.update_transaction(con, ids[p.payload["txn_id"]], category=p.payload["to"])
            done["recategorised"] += 1
    con.commit()
    return done
