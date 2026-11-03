"""Replays a full payroll year through the same pay-run engine, period by period.

Year-end slips (T4, RL-1) and the Record of Employment are built from pay runs, not
from a separate calculation. This module produces those pay runs for every semi-monthly
period of the year:

- periods before the current one: regular pay at each employee's pre-change rate, with
  hourly staff paid the hours in hours_history.csv (or 80 regular hours if there is none);
- the current period: the real pay run, with the adjustments read from this period's inbox;
- later periods (22 to 24 here): projected at the rates in force after this period's
  raises, with no other changes. The year-end outputs say which periods were projected.

Year-to-date amounts are accumulated from the lines themselves, so the YTD each run sees
is exactly what earlier runs deducted. A test checks that the replay reproduces the
year-to-date columns of employees.csv at the current period.
"""

from __future__ import annotations

import calendar
import copy
import csv
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from payroll import Inputs, PayRun, Period, build_pay_run
from payroll_calc import D, D0, YTD

DEFAULT_HOURLY_HOURS = (Decimal("80"), Decimal("0"))


def semi_monthly_periods(year: int) -> list[Period]:
    """24 periods: 1st-15th and 16th-month end. Pay date is the last weekday on or before
    the period end (15 Nov 2026 is a Sunday, so that period is paid Friday 13 Nov)."""
    out = []
    for m in range(1, 13):
        last = calendar.monthrange(year, m)[1]
        for n, (a, b) in enumerate([(1, 15), (16, last)]):
            start, end = date(year, m, a), date(year, m, b)
            pay = end
            while pay.weekday() >= 5:
                pay -= timedelta(days=1)
            out.append(Period(start, end, pay, 24, 2 * (m - 1) + n + 1))
    return out


def load_hours_history(data_dir: Path) -> dict[int, dict[str, tuple[Decimal, Decimal]]]:
    p = data_dir / "hours_history.csv"
    out: dict[int, dict[str, tuple[Decimal, Decimal]]] = {}
    if not p.exists():
        return out
    with p.open() as f:
        for r in csv.DictReader(f):
            out.setdefault(int(r["period"]), {})[r["employee_id"]] = (D(r["regular_hours"]), D(r["overtime_hours"]))
    return out


@dataclass
class PeriodRun:
    period: Period
    run: PayRun
    kind: str          # "history", "current" or "projected"


def _accumulate(ytd: YTD, line) -> YTD:
    d = line.deductions
    csb = D0
    if d.quebec:
        csb = d.detail["quebec_lump_sum"].get("CSB", D0)
    return YTD(pensionable=ytd.pensionable + line.gross, cpp=ytd.cpp + d.cpp, cpp2=ytd.cpp2 + d.cpp2,
               ei=ytd.ei + d.ei, insurable=ytd.insurable + line.gross, qpip=ytd.qpip + d.qpip,
               employer_qpip=ytd.employer_qpip + d.employer_qpip, lump_sums=ytd.lump_sums + line.bonus,
               csb=ytd.csb + csb)


def replay_year(inputs: Inputs, adjustments: list[dict], data_dir: Path, *, through: int | None = None) -> list[PeriodRun]:
    """Pay runs for periods 1..through (default: the whole year), starting from zero YTD.

    `inputs` is the current period's inputs (load_inputs); `adjustments` are this
    period's extracted adjustments. Raises keep applying in later periods."""
    year = inputs.period.pay_date.year
    current = inputs.period.number
    periods = semi_monthly_periods(year)
    through = through or len(periods)
    history_hours = load_hours_history(data_dir)
    raises = [a for a in adjustments if a.get("type") == "raise"]

    emps = [copy.deepcopy(e) for e in inputs.employees]
    for e in emps:
        e.ytd, e.ytd_gross, e.ytd_tax = YTD(), D0, D0

    out: list[PeriodRun] = []
    for per in periods[:through]:
        if per.number == current:
            kind, adjs, hours, default = "current", adjustments, inputs.hours, None
        elif per.number < current:
            kind, adjs, hours, default = "history", [], {}, {}
            for e in emps:
                if e.pay_type == "hourly":
                    default[e.id] = history_hours.get(per.number, {}).get(e.id, DEFAULT_HOURLY_HOURS)
        else:
            kind, adjs, hours, default = "projected", raises, {}, {}
            for e in emps:
                if e.pay_type == "hourly":
                    default[e.id] = history_hours.get(per.number, {}).get(e.id, DEFAULT_HOURLY_HOURS)
        run = build_pay_run(replace(inputs, period=per, employees=emps, hours=hours, default_hourly_hours=default),
                            [dict(a) for a in adjs])
        out.append(PeriodRun(per, run, kind))
        for line in run.lines:
            e = line.employee
            e.ytd = _accumulate(e.ytd, line)
            e.ytd_gross += line.gross
            e.ytd_tax += line.deductions.income_tax
    return out


def ytd_before(runs: list[PeriodRun], period_number: int) -> dict[str, dict[str, Decimal]]:
    """Year-to-date totals per employee before `period_number`, for checks."""
    out: dict[str, dict[str, Decimal]] = {}
    for pr in runs:
        if pr.period.number >= period_number:
            break
        for l in pr.run.lines:
            d = l.deductions
            t = out.setdefault(l.employee.id, {k: D0 for k in ["gross", "cpp", "cpp2", "ei", "qpip", "employer_qpip", "tax"]})
            t["gross"] += l.gross
            t["cpp"] += d.cpp
            t["cpp2"] += d.cpp2
            t["ei"] += d.ei
            t["qpip"] += d.qpip
            t["employer_qpip"] += d.employer_qpip
            t["tax"] += d.income_tax
    return out
