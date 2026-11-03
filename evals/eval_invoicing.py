"""Eval for 01_invoicing: per-field accuracy of customer-detail extraction.

Labelled set: data/synthetic/invoicing/eval/{cases,labels}.json, written by
01_invoicing/invoicing_data.py alongside the data (new-client onboarding threads, billing-change
notices and one non-billing email). A field counts as correct when the normalised prediction
equals the normalised label, including both being null.

Always scores the RulesExtractor. Scores the ClaudeExtractor too when AFO_LLM_MODE is live or record.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "01_invoicing"))

from shared.mail import read_mailbox  # noqa: E402

from invoicing_extract import (  # noqa: E402
    FIELDS, ClaudeExtractor, Email, ExtractionInput, RulesExtractor, normalize,
)

DATA = ROOT / "data" / "synthetic" / "invoicing"
SUPPLIER = "Tamarack Lane Advisory Inc."


def load_cases(data: Path = DATA) -> list[tuple[dict, ExtractionInput, dict]]:
    cases = json.loads((data / "eval" / "cases.json").read_text())
    labels = json.loads((data / "eval" / "labels.json").read_text())
    out = []
    for c in cases:
        emails = []
        for rel in c["emails"]:
            p = data / rel
            m = next(m for m in read_mailbox(p.parent) if m.path.name == p.name)
            emails.append(Email(m.sender, m.subject, m.date, m.body))
        inp = ExtractionInput(c["client_name"], SUPPLIER, c["contract_text"], emails)
        out.append((c, inp, labels[c["case_id"]]))
    return out


def score(extractor, cases) -> dict:
    correct = {f: 0 for f in FIELDS}
    errors = []
    exact = 0
    for c, inp, label in cases:
        pred = extractor.extract(inp)
        ok_all = True
        for f in FIELDS:
            if normalize(f, pred.get(f)) == normalize(f, label.get(f)):
                correct[f] += 1
            else:
                ok_all = False
                errors.append({"case": c["case_id"], "field": f, "expected": label.get(f), "got": pred.get(f)})
        exact += ok_all
    n = len(cases)
    per_field = {f: round(correct[f] / n, 3) for f in FIELDS}
    return {
        "n_cases": n,
        "per_field_accuracy": per_field,
        "mean_field_accuracy": round(sum(correct.values()) / (n * len(FIELDS)), 3),
        "all_fields_correct": round(exact / n, 3),
        "errors": errors,
    }


def run() -> dict:
    cases = load_cases()
    result = {"task": "customer-detail extraction", "rules": score(RulesExtractor(), cases)}
    if os.environ.get("AFO_LLM_MODE", "replay") in {"live", "record"}:
        result["claude"] = score(ClaudeExtractor(), cases)
    else:
        result["claude"] = "not run (AFO_LLM_MODE=replay; no recorded responses)"
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
