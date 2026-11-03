"""Record of Employment (ROE) data for an employee whose employment ended. SAMPLE ONLY.

Produces the data an employer would key into ROE Web, from the replayed pay runs
(payroll_history.replay_year). It is not an ROE and is not submitted anywhere.

Rules followed (Service Canada, "How to complete the record of employment (ROE) form",
https://www.canada.ca/en/employment-social-development/programs/ei/ei-list/reports/roe-guide.html):
- Pay period 1 (P.P. 1) is the final pay period, the one containing the last day for
  which paid; earlier periods count back from it. Nil periods are reported as 0.00.
- Electronic ROE, semi-monthly: block 15A totals insurable hours over the last 25 pay
  periods; block 15B totals insurable earnings over the last 13; block 15C lists
  insurable earnings for up to 25 periods.
- Periods before the first day worked are not part of the period of employment and are
  not listed.
- Block 16 reason codes: A shortage of work/layoff, E quit, M dismissal, and others.

Not modelled, and flagged for a person: vacation pay or other monies owing on separation
(block 17). Quebec's Act respecting labour standards sets vacation pay owing at
termination; a person must confirm the amount before the ROE is issued, and any insurable
separation payment is added to block 15B and to P.P. 1 in block 15C.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from payroll import Company, Employee
from payroll_calc import D0
from payroll_history import PeriodRun

LOOKBACK = {24: {"15A": 25, "15B": 13, "15C": 25}}   # semi-monthly
REASONS = {"A": "Shortage of work / end of contract or season", "E": "Quit", "M": "Dismissal or suspension",
           "K": "Other"}
SAMPLE = "SAMPLE - synthetic data - not an ROE, not for submission"


def roe_for(e: Employee, runs: list[PeriodRun], company: Company, reason: str = "A") -> dict:
    if e.end_date is None:
        raise ValueError(f"{e.id} has no end date")
    per_year = runs[0].period.periods_per_year
    lb = LOOKBACK[per_year]
    # Periods of employment, oldest first, ending with the one containing the last day paid.
    rows = []
    for pr in runs:
        p = pr.period
        if p.end < e.start_date or p.start > e.end_date:
            continue
        line = next((l for l in pr.run.lines if l.employee.id == e.id), None)
        rows.append({"period_number": p.number, "period_start": p.start.isoformat(), "period_end": p.end.isoformat(),
                     "insurable_earnings": line.gross if line else D0,
                     "insurable_hours": line.hours if line else D0})
    rows.reverse()
    for i, r in enumerate(rows, start=1):
        r["pay_period"] = i                               # P.P. 1 = final pay period
    final = rows[0]
    first_period_start = runs[0].period.start
    return {
        "label": SAMPLE,
        "block5_cra_business_number": company.payroll_account,
        "block6_pay_period_type": "Semi-monthly",
        "block8_sin": "".join(ch for ch in e.sin if ch.isdigit()),
        "block9_employee_name": e.name,
        "block10_first_day_worked": max(e.start_date, first_period_start).isoformat(),
        "block11_last_day_paid": e.end_date.isoformat(),
        "block12_final_pay_period_ending": final["period_end"],
        "block13_occupation": e.occupation,
        "block15A_total_insurable_hours": sum((r["insurable_hours"] for r in rows[:lb["15A"]]), D0),
        "block15B_total_insurable_earnings": sum((r["insurable_earnings"] for r in rows[:lb["15B"]]), D0),
        "block15C_insurable_earnings_by_pay_period": [
            {"pay_period": r["pay_period"], "period_end": r["period_end"],
             "insurable_earnings": r["insurable_earnings"], "insurable_hours": r["insurable_hours"]}
            for r in rows[:lb["15C"]]],
        "block16_reason_code": reason,
        "block16_reason": REASONS[reason],
        "block17_separation_payments": [],
        "block20_language": "F" if company.province == "QC" else "E",
        "flags": ["vacation pay owing on separation not modelled: confirm block 17A before issuing"]
                 + (["employment began before this year's data: block 10 and earlier periods need prior-year records"]
                    if e.start_date < first_period_start else []),
    }


def write_roe(roe: dict, folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"SAMPLE_roe_{roe['block9_employee_name'].replace(' ', '_')}"

    def js(x):
        if isinstance(x, Decimal):
            return str(x)
        raise TypeError(type(x))

    roe = {**roe, "flags": [f for f in roe["flags"] if f]}
    j = folder / f"{stem}.json"
    j.write_text(json.dumps(roe, indent=2, ensure_ascii=False, default=js) + "\n")
    c = folder / f"{stem}_15C.csv"
    with c.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"# {SAMPLE}"])
        w.writerow(["pay_period", "period_end", "insurable_earnings", "insurable_hours"])
        for r in roe["block15C_insurable_earnings_by_pay_period"]:
            w.writerow([r["pay_period"], r["period_end"], f"{r['insurable_earnings']:.2f}", r["insurable_hours"]])
    return j, c
