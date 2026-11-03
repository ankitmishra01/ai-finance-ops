"""Eval for 04_gst_hst: classification accuracy and GST34 exact match.

Scores the RulesExtractor (always) and the ClaudeExtractor (only when
AFO_LLM_MODE is live or record) against the gold labels generated with the
synthetic quarter. All numbers are computed here on each run.

- Classification: category, supply type, ITC treatment and (for sales) place of
  supply, on the 30-item labelled eval subset and on the full quarter. "raw" is
  what the extractor returned; "effective" is after the rules file resolves the
  category (what actually drives the return).
- Errors caught: of the eval-set items the extractor got wrong, the share that
  carry a low-confidence or rule-conflict flag, so a person would see them.
- GST34: the engine fed the gold labels must reproduce the generator's line
  totals exactly; the same comparison is reported for each extractor.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "04_gst_hst"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from gst_hst import (DATA_DIR, Rules, compute_gst34, gold_classifications, load_labels,  # noqa: E402
                     load_transactions, treat)
from gst_hst_extract import ClaudeExtractor, RulesExtractor, classify_all  # noqa: E402

SALES = {"sale_domestic", "sale_export", "sale_exempt_residential_rent", "credit_note"}


def _acc(pairs) -> dict:
    pairs = list(pairs)
    n = len(pairs)
    k = sum(1 for a, b in pairs if a == b)
    return {"correct": k, "n": n, "accuracy": round(k / n, 4) if n else None}


def _lines_report(computed: dict, expected: dict) -> dict:
    diffs = {k: {"computed": computed[k], "expected": v} for k, v in expected.items() if computed[k] != v}
    return {"exact_match": not diffs, "lines_matching": len(expected) - len(diffs), "lines_total": len(expected),
            "mismatches_cents": diffs}


def score(extractor, txns, labels, rules, expected) -> dict:
    preds = classify_all(extractor, txns)
    treated = {t.txn.txn_id: t for t in treat(txns, preds, rules)}
    out = {}
    for name, ids in (("eval_set", [k for k, r in labels.items() if r["in_eval_set"] == "1"]),
                      ("full_quarter", list(labels))):
        g = [labels[i] for i in ids]
        p = [preds[i] for i in ids]
        eff = [treated[i] for i in ids]
        sales = [(labels[i]["place_of_supply"], preds[i].place_of_supply) for i in ids
                 if labels[i]["category"] in SALES]
        out[name] = {
            "category": _acc((a["category"], b.category) for a, b in zip(g, p)),
            "supply_type_raw": _acc((a["supply_type"], b.supply_type) for a, b in zip(g, p)),
            "itc_treatment_raw": _acc((a["itc_treatment"], b.itc_treatment) for a, b in zip(g, p)),
            "supply_type_effective": _acc((a["supply_type"], b.supply_type) for a, b in zip(g, eff)),
            "itc_treatment_effective": _acc((a["itc_treatment"], b.itc_treatment) for a, b in zip(g, eff)),
            "place_of_supply_sales": _acc(sales),
        }
        wrong = [i for i in ids if preds[i].category != labels[i]["category"]
                 or (labels[i]["category"] in SALES and preds[i].place_of_supply != labels[i]["place_of_supply"])]
        caught = [i for i in wrong if any(f.startswith(("low confidence", "rule conflict", "place of supply"))
                                          for f in treated[i].flags)]
        out[name]["errors_flagged_for_human"] = {"errors": len(wrong), "flagged": len(caught),
                                                 "unflagged_ids": sorted(set(wrong) - set(caught))}
    out["gst34_vs_expected"] = _lines_report(compute_gst34(list(treated.values())), expected)
    return out


def run() -> dict:
    rules = Rules()
    txns = load_transactions()
    labels = load_labels()
    expected = json.loads((DATA_DIR / "expected_gst34.json").read_text())["lines"]

    result = {
        "n_transactions": len(txns),
        "n_eval_set": sum(1 for r in labels.values() if r["in_eval_set"] == "1"),
        "gst34_from_gold_labels": _lines_report(compute_gst34(treat(txns, gold_classifications(labels), rules)),
                                                expected),
        "rules_extractor": score(RulesExtractor(rules), txns, labels, rules, expected),
    }
    if os.environ.get("AFO_LLM_MODE", "replay") in ("live", "record"):
        result["claude_extractor"] = score(ClaudeExtractor(rules), txns, labels, rules, expected)
    else:
        result["claude_extractor"] = "not run (set AFO_LLM_MODE=live or record with ANTHROPIC_API_KEY)"
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
