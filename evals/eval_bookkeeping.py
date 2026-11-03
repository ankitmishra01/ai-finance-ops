"""Eval for 05_bookkeeping: scores the extractor + matcher against gold labels written by the
seeded generator (data/synthetic/bookkeeping/labels.json).

Reports:
  receipt_fields   per-field accuracy of receipt extraction (54 emails)
  matching         precision / recall / F1 of (receipt, transaction) links vs gold
  categorisation   accuracy vs gold account codes (80 transactions), and on the subset
                   whose current ledger category is wrong
  exceptions       orphan-receipt precision/recall, duplicate receipt detection
The ClaudeExtractor is scored too when AFO_LLM_MODE is live or record.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "05_bookkeeping"
for p in (str(ROOT), str(MOD)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bookkeeping as bk  # noqa: E402
from bookkeeping_extract import LOW_CONFIDENCE, ClaudeExtractor, RulesExtractor  # noqa: E402
from bookkeeping_match import merchant_similarity, norm_name  # noqa: E402


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def score(extractor, data_dir: Path = bk.DATA) -> dict:
    labels = json.loads((data_dir / "labels.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        res = bk.run(extractor, data_dir, Path(tmp))

    # Receipt fields
    gold_r = labels["receipts"]
    fields = {"merchant": 0, "date": 0, "total": 0, "gst_hst": 0, "currency": 0, "gst_number": 0}
    misses: dict[str, list[str]] = {k: [] for k in fields}
    for rid, g in gold_r.items():
        r = res.receipts[rid]
        ok = {
            "merchant": norm_name(r["merchant"]) == norm_name(g["merchant"])
            or merchant_similarity(r["merchant"], g["merchant"]) >= 0.9,
            "date": r["date"] == g["date"],
            "total": bk.to_cents(r["total"]) == g["total_cents"],
            "gst_hst": bk.to_cents(r["gst_hst"]) == g["gst_hst_cents"],
            "currency": r["currency"] == g["currency"],
            "gst_number": (r.get("gst_number") or None) == g["gst_number"],
        }
        for k, v in ok.items():
            fields[k] += v
            if not v:
                misses[k].append(rid)
    n_r = len(gold_r)
    receipt_fields = {k: _ratio(v, n_r) for k, v in fields.items()}
    receipt_fields["all_fields_correct"] = _ratio(
        sum(1 for rid in gold_r if not any(rid in m for m in misses.values())), n_r)
    receipt_fields["n"] = n_r

    # Matching
    gold_links = {tuple(x) for x in labels["links"]}
    pred_links = {(l.receipt_id, t) for l in res.matches.links for t in l.txn_ids}
    tp = len(gold_links & pred_links)
    p, r = _ratio(tp, len(pred_links)), _ratio(tp, len(gold_links))
    matching = {"precision": p, "recall": r,
                "f1": round(2 * p * r / (p + r), 4) if p and r else 0.0,
                "gold_links": len(gold_links), "predicted_links": len(pred_links), "true_positives": tp,
                "false_positives": sorted(map(list, pred_links - gold_links)),
                "false_negatives": sorted(map(list, gold_links - pred_links))}

    # Categorisation
    gold_c = labels["categories"]
    correct = [tid for tid, code in gold_c.items() if res.categories[tid]["account_code"] == code]
    wrong_now = labels["ledger_wrong"]
    confident = [tid for tid in gold_c if res.categories[tid]["confidence"] >= LOW_CONFIDENCE]
    categorisation = {
        "accuracy": _ratio(len(correct), len(gold_c)),
        "n": len(gold_c),
        "accuracy_when_confident": _ratio(sum(1 for t in confident if t in correct), len(confident)),
        "confident_share": _ratio(len(confident), len(gold_c)),
        "fixes_wrong_ledger_lines": _ratio(sum(1 for t in wrong_now if t in correct), len(wrong_now)),
        "n_wrong_ledger_lines": len(wrong_now),
        "current_ledger_accuracy": _ratio(
            sum(1 for t in res.txns if t["category"] == gold_c[t["txn_id"]]), len(gold_c)),
    }

    # Exceptions
    gold_orph = {rid for rid, g in gold_r.items() if g["orphan"]}
    pred_orph = set(res.matches.orphan_receipts) | set(res.matches.not_receipts)
    gold_dup = {rid for rid, g in gold_r.items() if g["duplicate_of"]}
    exceptions = {
        "orphan_precision": _ratio(len(gold_orph & pred_orph), len(pred_orph)),
        "orphan_recall": _ratio(len(gold_orph & pred_orph), len(gold_orph)),
        "duplicate_receipts_found": f"{len(gold_dup & set(res.matches.duplicate_receipts))}/{len(gold_dup)}",
    }
    return {"extractor": extractor.name, "receipt_fields": receipt_fields, "matching": matching,
            "categorisation": categorisation, "exceptions": exceptions}


def run() -> dict:
    chart = bk.load_chart()
    out = {"rules": score(RulesExtractor(chart))}
    if os.environ.get("AFO_LLM_MODE") in {"live", "record"}:
        out["claude"] = score(ClaudeExtractor(chart))
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
