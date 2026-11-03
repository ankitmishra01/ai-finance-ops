"""Eval for 02_bills: email classification accuracy, per-field extraction accuracy and flag recall.

Scored against data/synthetic/bills/labels.json, written by 02_bills/bills_data.py alongside the
mailbox. The RulesExtractor is always scored. The ClaudeExtractor is scored only when
AFO_LLM_MODE is live or record (it needs an API key; nothing is replayed from invented fixtures).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02_bills"))

from bills import norm_vendor, parse_terms, process  # noqa: E402
from bills_extract import ClaudeExtractor, RulesExtractor, to_cents  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

DATA = ROOT / "data" / "synthetic" / "bills"
FIELDS = ["vendor_name", "gst_number", "invoice_number", "invoice_date", "subtotal", "tax", "total",
          "currency", "terms", "category"]
FLAG_TYPES = {  # first match wins, so "possible duplicate" is tested before "duplicate:"
    "possible duplicate": "possible_duplicate", "duplicate:": "duplicate", "total mismatch": "total_mismatch",
    "tax rate inconsistent": "tax_rate", "foreign currency": "foreign_currency",
    "missing GST/HST registration": "missing_bn", "is not 9 digits": "invalid_bn",
    "fails the check digit": "invalid_bn", "missing fields": "missing_fields",
}


def flag_types(flags: list[str]) -> set[str]:
    return {next((t for k, t in FLAG_TYPES.items() if k in f), "other") for f in flags}


def _norm(field: str, v):
    if v is None or v == "":
        return None
    if field == "vendor_name":
        return norm_vendor(v)
    if field in ("gst_number", "invoice_number"):
        return re.sub(r"\s", "", str(v)).upper()
    if field == "terms":
        # Compare what the terms mean (net days, discount days), not how they are worded.
        days, disc, assumed = parse_terms(str(v))
        return None if assumed else (days, disc)
    return v


def _predicted(e, field: str):
    if field in ("subtotal", "tax", "total"):
        return {"subtotal": e.subtotal_cents, "tax": e.tax_cents, "total": e.total_cents}[field]
    return getattr(e, field)


def _expected(lab: dict, field: str):
    if field in ("subtotal", "tax", "total"):
        return to_cents(lab[field])
    return lab[field]


def _score_split(extracted, labels, keep) -> dict:
    items = [(src, e) for src, e in extracted if keep(labels[src])]
    correct = sum(e.doc_type == labels[src]["doc_type"] for src, e in items)
    confusion: dict[str, dict[str, int]] = {}
    for src, e in items:
        row = confusion.setdefault(labels[src]["doc_type"], {})
        row[e.doc_type] = row.get(e.doc_type, 0) + 1
    bills = [(src, e) for src, e in items if labels[src]["doc_type"] == "bill"]
    field_hits = {f: 0 for f in FIELDS}
    misses: list[str] = []
    for src, e in bills:
        lab = labels[src]
        for f in FIELDS:
            ok = e.doc_type == "bill" and _norm(f, _predicted(e, f)) == _norm(f, _expected(lab, f))
            field_hits[f] += ok
            if not ok:
                misses.append(f"{src}.{f}: got {_predicted(e, f)!r}, want {_expected(lab, f)!r}")
    n = len(bills)
    return {
        "messages": len(items),
        "classification_accuracy": round(correct / len(items), 4) if items else None,
        "classification_confusion": confusion,
        "bills_labelled": n,
        "field_accuracy": {f: round(h / n, 4) for f, h in field_hits.items()} if n else {},
        "field_accuracy_mean": round(sum(field_hits.values()) / (n * len(FIELDS)), 4) if n else None,
        "field_misses": misses,
    }


def score(extractor) -> dict:
    meta = json.loads((DATA / "labels.json").read_text())
    labels = {x["file"]: x for x in meta["items"]}
    mails = read_mailbox(DATA / "inbox")
    extracted = [(m.path.name, extractor.extract(m)) for m in mails]

    reviews = process(extracted, as_of=date.fromisoformat(meta["as_of"]),
                      recipient_province=meta["recipient_province"])
    got_flags = {r.source: flag_types(r.flags) for r in reviews}
    tp = fp = fn = 0
    for src, lab in labels.items():
        want = set(lab.get("expected_flags", []))
        got = got_flags.get(src, set())
        tp += len(want & got)
        fp += len(got - want)
        fn += len(want - got)

    return {
        "extractor": extractor.name,
        "all": {k: v for k, v in _score_split(extracted, labels, lambda lab: True).items()
                if k != "field_misses"},
        "templated": _score_split(extracted, labels, lambda lab: lab["split"] == "templated"),
        "holdout": _score_split(extracted, labels, lambda lab: lab["split"] == "holdout"),
        "flags": {"true_positive": tp, "false_positive": fp, "false_negative": fn,
                  "recall": round(tp / (tp + fn), 4) if tp + fn else None,
                  "precision": round(tp / (tp + fp), 4) if tp + fp else None},
    }


def run() -> dict:
    out = {"rules": score(RulesExtractor())}
    if os.environ.get("AFO_LLM_MODE") in ("live", "record"):
        out["claude"] = score(ClaudeExtractor())
    else:
        out["claude"] = "not run (set AFO_LLM_MODE=live or record with ANTHROPIC_API_KEY)"
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
