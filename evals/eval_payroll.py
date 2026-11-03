"""Payroll change-request extraction accuracy.

Scores an Extractor against data/synthetic/payroll/labels.json (21 hand-labelled emails:
6 in this period's inbox and 15 more in eval/). The labels were written with the data
generator (03_payroll/payroll_data.py), not produced by any model.

Metrics:
- email_exact_match: share of emails where the predicted adjustments equal the labels exactly
  (same count, type, employee and every field).
- adjustment precision / recall / f1: an adjustment matches when type and employee agree.
- field_accuracy: on matched adjustments, share of the 9 non-type fields that are equal
  (numbers within 0.005, None must equal None).

The RulesExtractor is always scored. The ClaudeExtractor is scored only when
AFO_LLM_MODE is live or record.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "03_payroll"))

from payroll import load_inputs, match_employee  # noqa: E402
from payroll_extract import FIELDS, ClaudeExtractor, RulesExtractor  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

DATA = ROOT / "data" / "synthetic" / "payroll"
COMPARED = [f for f in FIELDS if f != "type"]


def _who(name: str, employees) -> str:
    e = match_employee(name, employees)
    return e.id if e else (name or "").strip().lower()


def _eq(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.005
    return str(a).strip().lower() == str(b).strip().lower()


def score(extractor, data: Path = DATA) -> dict:
    inputs = load_inputs(data)
    emps = inputs.employees
    roster = [e.name for e in emps]
    labels = json.loads((data / "labels.json").read_text())
    mails = {f"{m.path.parent.name}/{m.path.name}": m
             for folder in ("inbox", "eval") for m in read_mailbox(data / folder)}

    tp = fp = fn = 0
    exact = 0
    fields_ok = fields_total = 0
    misses = []
    for key, expected in labels.items():
        pred = extractor.extract(mails[key], roster=roster, period_start=inputs.period.start)
        exp_k = {(a["type"], _who(a["employee_name"], emps)): a for a in expected}
        pred_k = {(a["type"], _who(a["employee_name"], emps)): a for a in pred}
        matched = set(exp_k) & set(pred_k)
        tp += len(matched)
        fp += len(pred) - len(matched)
        fn += len(exp_k) - len(matched)
        all_ok = len(pred) == len(expected) and len(matched) == len(expected)
        for k in matched:
            for f in COMPARED:
                if f == "employee_name":
                    continue  # already part of the match key
                fields_total += 1
                if _eq(exp_k[k].get(f), pred_k[k].get(f)):
                    fields_ok += 1
                else:
                    all_ok = False
        if all_ok:
            exact += 1
        else:
            misses.append(key)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "emails": len(labels),
        "labelled_adjustments": tp + fn,
        "email_exact_match": round(exact / len(labels), 3),
        "adjustment_precision": round(precision, 3),
        "adjustment_recall": round(recall, 3),
        "adjustment_f1": round(f1, 3),
        "field_accuracy": round(fields_ok / fields_total, 3) if fields_total else None,
        "missed_emails": misses,
    }


def run() -> dict:
    out = {"task": "change-request email -> structured pay adjustments", "rules": score(RulesExtractor())}
    if os.environ.get("AFO_LLM_MODE") in {"live", "record"}:
        out["claude"] = score(ClaudeExtractor())
    else:
        out["claude"] = "not scored (set AFO_LLM_MODE=live or record with ANTHROPIC_API_KEY)"
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
