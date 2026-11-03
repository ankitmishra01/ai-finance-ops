"""Pay-run assembly: period inputs + extracted adjustments -> gross pay -> deductions -> flagged lines.

Deterministic. The only model-dependent input is the list of adjustments, which the
Extractor produced from emails and which a person reviews with each pay line.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from payroll_calc import D, D0, QC, RATES, YTD, Deductions, PeriodInput, compute, money
from shared.approvals import ApprovalQueue

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic" / "payroll"
CHANGE_THRESHOLD = Decimal("0.20")
SALARIED_HOURS_PER_DAY = Decimal("7.5")   # insurable hours for salaried staff (ROE); an assumption


# ---------------------------------------------------------------- inputs

@dataclass
class Employee:
    id: str
    name: str
    email: str
    pay_type: str                 # salary | hourly
    annual_salary: Decimal
    hourly_rate: Decimal
    start_date: date
    sin: str
    td1_received: bool
    federal_claim: Decimal | None
    ontario_claim: Decimal | None
    institution: str
    transit: str
    account: str
    ytd: YTD
    ytd_gross: Decimal = D0
    ytd_tax: Decimal = D0
    province: str = "ON"          # province of employment (T4 box 10)
    quebec_claim: Decimal | None = None   # TP-1015.3-V line 7 amount (E); None = basic
    end_date: date | None = None  # last day for which paid, if employment ended
    occupation: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""


@dataclass
class Period:
    start: date
    end: date
    pay_date: date
    periods_per_year: int
    number: int                   # pay period number in the year


@dataclass
class Company:
    name: str
    short_name: str
    payroll_account: str          # CRA program account, synthetic
    originator_id: str
    address: str
    province: str = "ON"
    rq_id: str = ""               # Revenu Québec identification number, synthetic
    street: str = ""
    city: str = ""
    postal_code: str = ""
    contact_name: str = ""
    contact_phone: str = ""       # "905-555-0100"
    contact_email: str = ""


@dataclass
class Inputs:
    company: Company
    period: Period
    employees: list[Employee]
    hours: dict[str, tuple[Decimal, Decimal]]   # id -> (regular, overtime)
    # Hours for hourly staff with no timesheet row; used only when replaying past periods.
    default_hourly_hours: dict[str, tuple[Decimal, Decimal]] | None = None


def _opt(v: str) -> Decimal | None:
    return D(v) if v not in ("", None) else None


def load_inputs(data_dir: Path = DATA) -> Inputs:
    cfg = yaml.safe_load((data_dir / "company.yaml").read_text())
    c, p = cfg["company"], cfg["period"]
    company = Company(c["name"], c["short_name"], c["payroll_account"], c["originator_id"], c["address"],
                      province=c.get("province", "ON"), rq_id=c.get("rq_id", ""), street=c.get("street", ""),
                      city=c.get("city", ""), postal_code=c.get("postal_code", ""),
                      contact_name=c.get("contact_name", ""), contact_phone=c.get("contact_phone", ""),
                      contact_email=c.get("contact_email", ""))
    period = Period(date.fromisoformat(p["start"]), date.fromisoformat(p["end"]),
                    date.fromisoformat(p["pay_date"]), int(p["periods_per_year"]), int(p["number"]))
    emps = []
    with (data_dir / "employees.csv").open() as f:
        for r in csv.DictReader(f):
            emps.append(Employee(
                id=r["id"], name=r["name"], email=r["email"], pay_type=r["pay_type"],
                annual_salary=D(r["annual_salary"] or 0), hourly_rate=D(r["hourly_rate"] or 0),
                start_date=date.fromisoformat(r["start_date"]), sin=r["sin"],
                td1_received=r["td1_received"] == "yes",
                federal_claim=_opt(r["td1_federal_claim"]), ontario_claim=_opt(r["td1_ontario_claim"]),
                institution=r["institution"], transit=r["transit"], account=r["account"],
                ytd=YTD(pensionable=D(r["ytd_pensionable"]), cpp=D(r["ytd_cpp"]), cpp2=D(r["ytd_cpp2"]),
                        ei=D(r["ytd_ei"]), insurable=D(r["ytd_insurable"]),
                        qpip=D(r.get("ytd_qpip") or 0), employer_qpip=D(r.get("ytd_employer_qpip") or 0)),
                ytd_gross=D(r["ytd_gross"]), ytd_tax=D(r["ytd_tax"]),
                province=r.get("province") or company.province,
                quebec_claim=_opt(r.get("tp1015_3_claim", "")),
                end_date=date.fromisoformat(r["end_date"]) if r.get("end_date") else None,
                occupation=r.get("occupation", ""), address=r.get("address", ""),
                city=r.get("city", ""), postal_code=r.get("postal_code", ""),
            ))
    hours = {}
    with (data_dir / "timesheet.csv").open() as f:
        for r in csv.DictReader(f):
            hours[r["employee_id"]] = (D(r["regular_hours"]), D(r["overtime_hours"]))
    return Inputs(company, period, emps, hours)


# ---------------------------------------------------------------- helpers

def working_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def sin_valid(sin: str) -> bool:
    """Format and Luhn check only. Synthetic SINs in this repo start with 0, a prefix the
    federal government does not issue to individuals, so they cannot belong to anyone."""
    digits = [int(ch) for ch in sin if ch.isdigit()]
    if len(digits) != 9:
        return False
    total = 0
    for i, n in enumerate(digits):
        if i % 2 == 1:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return total % 10 == 0


def match_employee(name: str, employees: list[Employee]) -> Employee | None:
    n = (name or "").strip().lower()
    if not n:
        return None
    for e in employees:
        if e.name.lower() == n:
            return e
    hits = [e for e in employees if any(part in e.name.lower().split() for part in n.split())]
    return hits[0] if len(hits) == 1 else None


def _adj_key(a: dict) -> tuple:
    return tuple((k, json.dumps(a.get(k))) for k in sorted(a) if k not in {"evidence", "source"})


# ---------------------------------------------------------------- pay lines

@dataclass
class PayLine:
    employee: Employee
    earnings: list[tuple[str, Decimal]]
    regular: Decimal
    bonus: Decimal
    baseline: Decimal | None
    deductions: Deductions | None = None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    hours: Decimal = D0           # insurable hours for the period (salaried: 7.5 h per day paid)

    @property
    def gross(self) -> Decimal:
        return self.regular + self.bonus

    @property
    def net(self) -> Decimal:
        return self.gross - self.deductions.employee_total

    def payload(self) -> dict:
        d = self.deductions
        return {
            "employee_id": self.employee.id, "name": self.employee.name,
            "earnings": [[k, str(v)] for k, v in self.earnings],
            "gross": str(self.gross), "regular": str(self.regular), "bonus": str(self.bonus),
            "province": d.province,
            "cpp": str(d.cpp), "cpp2": str(d.cpp2), "ei": str(d.ei),
            "federal_tax": str(d.federal_tax), "ontario_tax": str(d.ontario_tax),
            "employer_cpp": str(d.employer_cpp), "employer_cpp2": str(d.employer_cpp2),
            "employer_ei": str(d.employer_ei), "net": str(self.net),
            "notes": self.notes, "adjustments": self.adjustments,
        } | ({"qpp": str(d.cpp), "qpp2": str(d.cpp2), "qpip": str(d.qpip), "quebec_tax": str(d.provincial_tax),
               "employer_qpip": str(d.employer_qpip), "employer_hsf": str(d.employer_hsf)} if d.quebec else {})


@dataclass
class PayRun:
    inputs: Inputs
    lines: list[PayLine]
    exceptions: list[dict]        # adjustments that could not be applied to anyone


def build_pay_run(inputs: Inputs, adjustments: list[dict]) -> PayRun:
    """adjustments: extractor output, each with a `source` key naming the email file."""
    per = inputs.period
    P = per.periods_per_year
    wd = working_days(per.start, per.end)
    exceptions: list[dict] = []

    # De-duplicate identical requests (e.g. a forwarded copy of the same email).
    seen: dict[tuple, str] = {}
    unique, dupes = [], []
    for a in adjustments:
        k = _adj_key(a)
        if k in seen:
            dupes.append((a, seen[k]))
        else:
            seen[k] = a.get("source", "")
            unique.append(a)

    by_emp: dict[str, list[dict]] = {}
    for a in unique:
        e = match_employee(a.get("employee_name", ""), inputs.employees)
        if e is None:
            exceptions.append({**a, "reason": "employee not found in roster" +
                               (" (new hire must be set up with banking, SIN and TD1 first)" if a["type"] == "new_hire" else "")})
            continue
        by_emp.setdefault(e.id, []).append(a)
    dup_by_emp: dict[str, list[str]] = {}
    for a, first in dupes:
        e = match_employee(a.get("employee_name", ""), inputs.employees)
        if e:
            dup_by_emp.setdefault(e.id, []).append(
                f"duplicate {a['type']} request in {a.get('source')} (same as {first}); counted once")

    lines = []
    for e in inputs.employees:
        adjs = by_emp.get(e.id, [])
        line = _gross_for(e, adjs, inputs, wd, P)
        if line is None:
            continue
        line.flags += dup_by_emp.get(e.id, [])
        line.deductions = compute(PeriodInput(
            periods_per_year=P, regular=line.regular, bonus=line.bonus,
            federal_claim=e.federal_claim if e.td1_received else None,
            ontario_claim=e.ontario_claim if e.td1_received else None,
            quebec_claim=e.quebec_claim if e.td1_received else None,
            province=e.province, ytd=e.ytd))
        _flag(line, inputs)
        lines.append(line)
    return PayRun(inputs, lines, exceptions)


def _gross_for(e: Employee, adjs: list[dict], inputs: Inputs, wd: list[date], P: int) -> PayLine | None:
    per = inputs.period
    if e.start_date > per.end or (e.end_date and e.end_date < per.start):
        return None
    notes: list[str] = []
    flags: list[str] = []
    earnings: list[tuple[str, Decimal]] = []
    bonus = D0

    # Rate in force on each working day of the period.
    def rate_on(day: date, base: Decimal) -> Decimal:
        r = base
        for a in adjs:
            if a["type"] != "raise":
                continue
            eff = date.fromisoformat(a["effective_date"]) if a.get("effective_date") else per.start
            new = a.get("new_annual_salary") if e.pay_type == "salary" else a.get("new_hourly_rate")
            if new is None:
                continue
            if eff <= day:
                r = D(new)
        return r

    for a in adjs:
        if a["type"] == "raise":
            eff = a.get("effective_date")
            new = a.get("new_annual_salary") if e.pay_type == "salary" else a.get("new_hourly_rate")
            if new is None:
                flags.append(f"raise request without a {'salary' if e.pay_type == 'salary' else 'rate'} figure; not applied")
            elif eff and date.fromisoformat(eff) > per.end:
                flags.append(f"raise effective {eff} is after this period; not applied")
            else:
                old = e.annual_salary if e.pay_type == "salary" else e.hourly_rate
                notes.append(f"raise {old} -> {new} effective {eff or per.start.isoformat()}")

    worked = [d for d in wd if d >= e.start_date and (e.end_date is None or d <= e.end_date)]
    new_hire = e.start_date > per.start
    leaving = e.end_date is not None and e.end_date < per.end
    hours = D0

    if e.pay_type == "salary":
        if new_hire:
            notes.append(f"new hire, prorated {len(worked)}/{len(wd)} working days from {e.start_date}")
        if leaving:
            notes.append(f"final pay, prorated {len(worked)}/{len(wd)} working days to {e.end_date}")
        # Period salary split across working days, each day at the rate then in force.
        # Rounded once, after leave, so the earnings lines add up to the cent.
        salary = sum((rate_on(d, e.annual_salary) / P / len(wd) for d in worked), D0)
        earnings.append(("Salary", money(salary)))
        unrounded = salary
        for a in adjs:
            if a["type"] == "unpaid_leave":
                days = D(a.get("unpaid_days") or 0)
                if days <= 0:
                    flags.append("unpaid leave request without a number of days; not applied")
                    continue
                if days > len(worked):
                    flags.append(f"unpaid leave of {days} days exceeds {len(worked)} working days in period")
                    days = D(len(worked))
                before = money(unrounded)
                unrounded -= rate_on(per.end, e.annual_salary) / P / len(wd) * days
                earnings.append((f"Unpaid leave ({days} days)", money(unrounded) - before))
            elif a["type"] == "hours_correction":
                flags.append("hours correction requested for a salaried employee; not applied")
        regular = money(unrounded)
        baseline = None if (new_hire or leaving) else money(e.annual_salary / P)
        unpaid = sum((D(a.get("unpaid_days") or 0) for a in adjs if a["type"] == "unpaid_leave"), D0)
        hours = max(D0, len(worked) - unpaid) * SALARIED_HOURS_PER_DAY
    else:
        reg_h, ot_h = inputs.hours.get(e.id, (None, None))
        if reg_h is None and inputs.default_hourly_hours is not None:
            reg_h, ot_h = inputs.default_hourly_hours.get(e.id, (None, None))
        if reg_h is None:
            flags.append("hourly employee has no timesheet row; paid $0")
            reg_h, ot_h = D0, D0
        rate = rate_on(per.end, e.hourly_rate)
        if any(a["type"] == "raise" and a.get("effective_date") and per.start < date.fromisoformat(a["effective_date"]) <= per.end for a in adjs):
            # No daily timesheet: split hours evenly across working days.
            per_day = reg_h / len(worked) if worked else D0
            reg_pay = money(sum((per_day * rate_on(d, e.hourly_rate) for d in worked), D0))
            notes.append("mid-period raise: hours split evenly across working days")
        else:
            reg_pay = money(reg_h * rate)
        earnings.append((f"Regular {reg_h} h @ {rate}", reg_pay))
        regular = reg_pay
        hours = reg_h + ot_h
        if ot_h:
            ot = money(ot_h * rate * Decimal("1.5"))
            earnings.append((f"Overtime {ot_h} h @ 1.5x", ot))
            regular += ot
        for a in adjs:
            if a["type"] == "hours_correction":
                h = D(a.get("extra_hours") or 0)
                hours += h
                amt = money(h * rate)
                earnings.append((f"Hours correction {h} h", amt))
                regular += amt
            elif a["type"] == "unpaid_leave":
                notes.append("unpaid leave for an hourly employee: timesheet hours already exclude it; no deduction")
        baseline = None if new_hire else money(reg_h * e.hourly_rate + ot_h * e.hourly_rate * Decimal("1.5"))

    for a in adjs:
        if a["type"] == "bonus":
            amt = D(a.get("bonus_amount") or 0)
            if amt <= 0:
                flags.append("bonus request without an amount; not applied")
                continue
            earnings.append(("Bonus", money(amt)))
            bonus += money(amt)
        elif a["type"] == "new_hire":
            if a.get("effective_date") and a["effective_date"] != e.start_date.isoformat():
                flags.append(f"start date in email ({a['effective_date']}) differs from employee record ({e.start_date})")
            if a.get("new_annual_salary") and e.pay_type == "salary" and D(a["new_annual_salary"]) != e.annual_salary:
                flags.append(f"salary in email ({a['new_annual_salary']}) differs from employee record ({e.annual_salary})")

    line = PayLine(e, earnings, regular, bonus, baseline, flags=flags, notes=notes,
                   adjustments=[{k: v for k, v in a.items()} for a in adjs], hours=hours)
    return line


def _flag(line: PayLine, inputs: Inputs) -> None:
    e, d = line.employee, line.deductions
    if line.baseline:
        change = (line.gross - line.baseline) / line.baseline
        if abs(change) > CHANGE_THRESHOLD:
            line.flags.append(f"gross changes {change:+.0%} vs normal period pay {line.baseline} (threshold 20%)")
    if e.start_date > inputs.period.start:
        if not sin_valid(e.sin):
            line.flags.append("new hire: SIN missing or invalid")
        if not e.td1_received:
            line.flags.append("new hire: TD1/TD1ON not received; basic personal amounts used (claim code 1)")
    elif not sin_valid(e.sin):
        line.flags.append("SIN missing or fails check digit")
    plan = "QPP" if d.quebec else "CPP"
    if d.cpp_max_reached:
        line.flags.append(f"reaches {plan} annual maximum this period ({plan} {d.cpp})")
    if d.cpp2_max_reached:
        line.flags.append(f"reaches {plan}2 annual maximum this period ({plan}2 {d.cpp2})")
    if d.ei_max_reached:
        line.flags.append(f"reaches EI annual maximum this period (EI {d.ei})")
    if d.qpip_max_reached:
        line.flags.append(f"reaches QPIP annual maximum this period (QPIP {d.qpip})")
    if not (e.institution and e.transit and e.account):
        line.flags.append("no direct-deposit banking details")
    if line.net <= 0:
        line.flags.append(f"net pay is {line.net}")
    rr = QC if d.quebec else RATES
    if e.ytd.cpp >= rr["qpp" if d.quebec else "cpp"]["max_contribution"] and d.cpp == 0:
        line.notes.append(f"{plan} already at annual maximum; no {plan} deducted")
    if e.ytd.ei >= rr["ei"]["max_premium"] and d.ei == 0:
        line.notes.append("EI already at annual maximum; no EI deducted")
    if d.quebec and e.ytd.qpip >= QC["qpip"]["max_premium"] and d.qpip == 0:
        line.notes.append("QPIP already at annual maximum; no QPIP deducted")


def propose(run: PayRun, queue: ApprovalQueue) -> None:
    per = run.inputs.period
    for line in run.lines:
        d = line.deductions
        qpip = f" - QPIP {d.qpip}" if d.quebec else ""
        summary = (f"{line.employee.name}: gross {line.gross} - {'QPP' if d.quebec else 'CPP'} {d.cpp + d.cpp2} "
                   f"- EI {d.ei}{qpip} - tax {d.income_tax} = net {line.net} (pay date {per.pay_date})")
        queue.propose("pay_line", summary, line.payload(), flags=list(line.flags))
    for x in run.exceptions:
        queue.propose("pay_exception", f"Unapplied {x['type']} for '{x.get('employee_name')}' from {x.get('source')}",
                      x, flags=[x["reason"]])


def totals(lines: list[PayLine]) -> dict[str, Decimal]:
    t = {k: D0 for k in ["gross", "cpp", "cpp2", "ei", "income_tax", "employer_cpp", "employer_cpp2",
                         "employer_ei", "net", "remittance", "qpip", "employer_qpip", "employer_hsf",
                         "federal_tax", "provincial_tax", "cra_remittance", "rq_remittance"]}
    for l in lines:
        d = l.deductions
        t["gross"] += l.gross
        t["cpp"] += d.cpp
        t["cpp2"] += d.cpp2
        t["ei"] += d.ei
        t["income_tax"] += d.income_tax
        t["employer_cpp"] += d.employer_cpp
        t["employer_cpp2"] += d.employer_cpp2
        t["employer_ei"] += d.employer_ei
        t["net"] += l.net
        t["remittance"] += d.remittance
        t["qpip"] += d.qpip
        t["employer_qpip"] += d.employer_qpip
        t["employer_hsf"] += d.employer_hsf
        t["federal_tax"] += d.federal_tax
        t["provincial_tax"] += d.provincial_tax
        t["cra_remittance"] += d.cra_remittance
        t["rq_remittance"] += d.rq_remittance
    return t
