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
AFO_LLM_MODE is live or record. The Montreal inbox (data/synthetic/payroll_qc, 2 emails)
is scored separately so the original 21-email figures stay comparable.

Deductions: evals/payroll_deduction_cases.json holds expected figures from outside this
code (CRA PDOC, Revenu Québec's TP-1015.F-V worked examples, one hand calculation). Each
field must match to the cent. The same figures are unit tests, so a perfect score means
"no regression against external figures", not generalisation.

Year-end: reconciliation checks on the replayed 2026 year for both employers (T4 Summary
line 80 vs remittances, RL-1 totals vs Revenu Québec remittances, ROE block 15B).
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
QC_DATA = ROOT / "data" / "synthetic" / "payroll_qc"
CASES = ROOT / "evals" / "payroll_deduction_cases.json"
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


def score_deductions(path: Path = CASES) -> dict:
    from decimal import Decimal as Dec

    from payroll_calc import (PeriodInput, compute, qpp2_contribution, qpp_contribution, quebec_tax_lump_sum,
                              quebec_tax_regular)

    cases = json.loads(path.read_text())["cases"]
    fields_ok = fields_total = cases_ok = 0
    misses = []
    for c in cases:
        i = {k: (v if k == "province" else Dec(v)) for k, v in c["input"].items()}
        if c["kind"] == "period":
            d = compute(PeriodInput(int(i["P"]), i["regular"], province=i["province"],
                                    federal_claim=i.get("federal_claim"), ontario_claim=i.get("ontario_claim")))
            got = {k: getattr(d, k) for k in c["expected"]}
        elif c["kind"] == "quebec_regular":
            got = quebec_tax_regular(P=int(i.pop("P")), **i)
        elif c["kind"] == "quebec_lump_sum":
            got = quebec_tax_lump_sum(P=int(i.pop("P")), **i)
        else:
            got = {"C": qpp_contribution(i["S3"], int(i["P"]), i["A5"])[0],
                   "C2": qpp2_contribution(i["S3"], i["S5"], i["A8"])[0]}
        ok = True
        for k, v in c["expected"].items():
            fields_total += 1
            if Dec(str(got[k])) == Dec(v):
                fields_ok += 1
            else:
                ok = False
                misses.append(f"{c['id']}.{k}: expected {v}, got {got[k]}")
        cases_ok += ok
    return {"cases": len(cases), "cases_exact": cases_ok, "fields": fields_total, "fields_exact": fields_ok,
            "misses": misses}


def score_yearend() -> dict:
    from payroll_roe import roe_for
    from payroll_yearend import build_year, rl1_slips, rl1_totals, t4_slips, t4_summary

    out = {}
    for name, data in (("ontario", DATA), ("montreal", QC_DATA)):
        inputs, runs = build_year(data, "rules")
        slips = t4_slips(runs)
        summ = t4_summary(runs, slips, inputs.company, inputs.period.pay_date.year)
        rl1 = rl1_slips(runs)
        tot = rl1_totals(runs, rl1)
        by_id = {s["employee_id"]: s for s in slips}
        checks = {
            "t4_line80_within_2_dollars_of_remittances": abs(
                summ["line80_total_deductions"] - summ["line82_remittances"]) <= 2,
            "t4_line14_equals_pay_runs": summ["line14_employment_income"] == sum(
                l.gross for pr in runs for l in pr.run.lines),
            "one_slip_per_employee_paid": len(slips) == len({l.employee.id for pr in runs for l in pr.run.lines}),
        }
        if tot:
            checks["rl1_totals_equal_rq_remittances"] = tot["total_due_to_revenu_quebec"] == tot["remitted_in_year"]
            checks["rl1_box_a_equals_t4_box_14"] = all(
                r["A_employment_income"] == by_id[r["employee_id"]]["box14_employment_income"] for r in rl1)
        roes = [roe_for(e, runs, inputs.company) for e in inputs.employees if e.end_date]
        for r in roes:
            pp = r["block15C_insurable_earnings_by_pay_period"]
            checks["roe_15B_equals_last_13_periods"] = r["block15B_total_insurable_earnings"] == sum(
                p["insurable_earnings"] for p in pp[:13])
        out[name] = {"pay_runs": len(runs), "t4_slips": len(slips), "rl1_slips": len(rl1), "roes": len(roes),
                     "t4_line80_minus_line82": str(summ["line80_total_deductions"] - summ["line82_remittances"]),
                     "checks_passed": sum(checks.values()), "checks": len(checks),
                     "failed": [k for k, v in checks.items() if not v]}
    return out


def run() -> dict:
    out = {"task": "change-request email -> structured pay adjustments", "rules": score(RulesExtractor()),
           "rules_montreal_inbox": score(RulesExtractor(), QC_DATA),
           "deductions_vs_external_figures": score_deductions(),
           "yearend_reconciliation": score_yearend()}
    if os.environ.get("AFO_LLM_MODE") in {"live", "record"}:
        out["claude"] = score(ClaudeExtractor())
    else:
        out["claude"] = "not scored (set AFO_LLM_MODE=live or record with ANTHROPIC_API_KEY)"
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
