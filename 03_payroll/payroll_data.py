"""Generates the synthetic payroll dataset (Faker en_CA, seed 42).

Writes to data/synthetic/payroll/:
  company.yaml       employer, pay period, pay date
  employees.csv      roster with TD1 claims, synthetic banking and year-to-date amounts
  timesheet.csv      hours for hourly staff this period
  inbox/*.eml        the change-request emails for this pay run
  eval/*.eml         more labelled change-request emails for the extraction eval
  labels.json        hand-written expected adjustments for every email in inbox/ and eval/

Everything is fictitious. SINs start with 0, which is not issued to individuals; the
institution number 999 is not a real bank; email domains are .test.

Year-to-date amounts are produced by running this module's own deduction engine over
pay periods 1-20 at each employee's pre-change rate, so they are internally consistent.

Run: uv run python 03_payroll/payroll_data.py
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from faker import Faker  # noqa: E402

from payroll_calc import YTD, PeriodInput, compute, money  # noqa: E402
from shared.mail import build_eml  # noqa: E402

OUT = ROOT / "data" / "synthetic" / "payroll"
DOMAIN = "lakeview-sample.test"
P = 24
PRIOR_PERIODS = 20          # Jan 1 - Oct 31 at 24 periods a year
STD_HOURS = Decimal("80")   # assumed hours per prior period for hourly staff

# id, pay_type, annual salary or hourly rate, start date, TD1 federal / Ontario claim (blank = basic)
ROSTER = [
    ("E01", "salary", "72000", "2023-04-03", "", ""),          # raise to 78,000 from Nov 1
    ("E02", "salary", "145000", "2021-09-07", "", ""),         # over the CPP, CPP2 and EI maximums
    ("E03", "salary", "85000", "2022-01-10", "", ""),          # reaches the CPP maximum this period
    ("E04", "salary", "80000", "2024-02-05", "", ""),          # 2 days unpaid leave; reaches EI maximum
    ("E05", "salary", "68000", "2020-06-15", "31000", "23000"),  # TD1 above basic (spouse amount)
    ("E06", "salary", "95000", "2019-11-18", "", ""),          # $1,500 bonus
    ("E07", "hourly", "31.00", "2022-08-22", "", ""),          # 6 hours added back
    ("E08", "hourly", "24.50", "2025-03-03", "", ""),          # 4 overtime hours
    ("E09", "salary", "62000", "2026-11-09", "", ""),          # new hire, SIN and TD1 missing
]


def luhn_sin(rng: random.Random) -> str:
    """9-digit number starting with 0 that passes the SIN check digit."""
    body = [0] + [rng.randint(0, 9) for _ in range(7)]
    for check in range(10):
        digits = body + [check]
        total = 0
        for i, n in enumerate(digits):
            if i % 2 == 1:
                n *= 2
                n = n - 9 if n > 9 else n
            total += n
        if total % 10 == 0:
            s = "".join(map(str, digits))
            return f"{s[:3]}-{s[3:6]}-{s[6:]}"
    raise AssertionError


def simulate_ytd(pay_type: str, amount: Decimal, fed: str, on: str) -> tuple[YTD, Decimal, Decimal]:
    ytd = YTD()
    gross_total = tax_total = Decimal("0")
    per_period = money(amount / P) if pay_type == "salary" else money(amount * STD_HOURS)
    for _ in range(PRIOR_PERIODS):
        d = compute(PeriodInput(P, per_period, federal_claim=Decimal(fed) if fed else None,
                                ontario_claim=Decimal(on) if on else None, ytd=ytd))
        ytd = YTD(pensionable=ytd.pensionable + per_period, cpp=ytd.cpp + d.cpp, cpp2=ytd.cpp2 + d.cpp2,
                  ei=ytd.ei + d.ei, insurable=ytd.insurable + per_period)
        gross_total += per_period
        tax_total += d.income_tax
    return ytd, gross_total, tax_total


def eml(sender: str, subject: str, date: str, body: str) -> bytes:
    return build_eml(sender=sender, to=f"payroll@{DOMAIN}", subject=subject, date=date, body=body)


def adj(type_, name, **kw) -> dict:
    d = {"type": type_, "employee_name": name, "effective_date": None, "bonus_amount": None,
         "new_annual_salary": None, "new_hourly_rate": None, "unpaid_days": None, "extra_hours": None,
         "sin_provided": None, "td1_provided": None}
    d.update(kw)
    return d


def main() -> None:
    fake = Faker("en_CA")
    Faker.seed(42)
    rng = random.Random(42)

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "inbox").mkdir(parents=True)
    (OUT / "eval").mkdir(parents=True)

    names = []
    while len(names) < len(ROSTER) + 3:
        n = f"{fake.first_name()} {fake.last_name()}"
        if n.split()[0] not in {x.split()[0] for x in names} and n.split()[1] not in {x.split()[1] for x in names}:
            names.append(n)
    roster_names, extra_new = names[: len(ROSTER)], names[len(ROSTER):]
    N = {r[0]: roster_names[i] for i, r in enumerate(ROSTER)}
    first = {k: v.split()[0] for k, v in N.items()}

    (OUT / "company.yaml").write_text(
        "# Synthetic employer and pay period. Not a real business.\n"
        "company:\n"
        "  name: Lakeview Sample Works Inc.\n"
        "  short_name: LAKEVIEW SAMPLE\n"
        "  payroll_account: 000000000RP0001\n"
        "  originator_id: SAMPLE0001\n"
        "  address: 100 Example Street, Hamilton ON L0L 0L0\n"
        "period:\n"
        "  start: '2026-11-01'\n"
        "  end: '2026-11-15'\n"
        "  pay_date: '2026-11-13'\n"
        "  periods_per_year: 24\n"
        "  number: 21\n"
    )

    with (OUT / "employees.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "email", "pay_type", "annual_salary", "hourly_rate", "start_date", "sin",
                    "td1_received", "td1_federal_claim", "td1_ontario_claim", "institution", "transit", "account",
                    "ytd_gross", "ytd_pensionable", "ytd_insurable", "ytd_cpp", "ytd_cpp2", "ytd_ei", "ytd_tax"])
        for (eid, ptype, amount, start, fed, on) in ROSTER:
            name = N[eid]
            email = f"{name.lower().replace(' ', '.').replace(chr(39), '')}@{DOMAIN}"
            new_hire = start >= "2026-11-01"
            sin = "" if new_hire else luhn_sin(rng)
            transit = f"{rng.randint(10000, 99999)}"
            account = f"{rng.randint(1000000, 9999999)}"
            if new_hire:
                ytd, g, t = YTD(), Decimal("0"), Decimal("0")
            else:
                ytd, g, t = simulate_ytd(ptype, Decimal(amount), fed, on)
            w.writerow([eid, name, email, ptype, amount if ptype == "salary" else "",
                        amount if ptype == "hourly" else "", start, sin, "no" if new_hire else "yes",
                        fed, on, "999", transit, account, g, ytd.pensionable, ytd.insurable,
                        ytd.cpp, ytd.cpp2, ytd.ei, t])

    with (OUT / "timesheet.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee_id", "regular_hours", "overtime_hours"])
        w.writerow(["E07", "80", "0"])
        w.writerow(["E08", "76", "4"])

    mgr = f"ops.manager@{DOMAIN}"
    hr = f"people@{DOMAIN}"
    labels: dict[str, list[dict]] = {}

    def put(folder: str, fname: str, sender: str, subject: str, day: str, body: str, expected: list[dict]):
        (OUT / folder / fname).write_bytes(eml(sender, subject, f"{day} -0500", body))
        labels[f"{folder}/{fname}"] = expected

    # ----- this period's inbox
    put("inbox", "01_raise.eml", mgr, f"Raise for {first['E01']}", "Mon, 26 Oct 2026 10:12:00",
        f"Hi,\n\nPlease process {first['E01']}'s raise. Salary goes to $78,000 effective Nov 1.\n\nThanks",
        [adj("raise", N["E01"], effective_date="2026-11-01", new_annual_salary=78000)])
    put("inbox", "02_unpaid_leave.eml", mgr, "Unpaid days", "Mon, 09 Nov 2026 08:40:00",
        f"Hello,\n\n{N['E04']} took 2 unpaid days on Nov 5 and Nov 6 for a family matter. Please deduct them this pay.\n",
        [adj("unpaid_leave", N["E04"], effective_date="2026-11-05", unpaid_days=2)])
    put("inbox", "03_new_hire.eml", hr, f"New hire - {N['E09']}", "Fri, 30 Oct 2026 15:05:00",
        f"Welcome aboard: {N['E09']} starts 11-09 as a production planner at $62,000 a year.\n"
        f"Banking form is in. SIN and TD1 forms are still to follow.\n",
        [adj("new_hire", N["E09"], effective_date="2026-11-09", new_annual_salary=62000,
             sin_provided=False, td1_provided=False)])
    bonus_body = (f"Please add a one-time bonus of $1,500 for {first['E06']} on this run. "
                  f"Great work closing out the year-end audit.\n")
    put("inbox", "04_bonus.eml", mgr, f"Bonus - {first['E06']}", "Tue, 03 Nov 2026 11:30:00", bonus_body,
        [adj("bonus", N["E06"], bonus_amount=1500)])
    put("inbox", "05_bonus_fwd.eml", hr, f"Fwd: Bonus - {first['E06']}", "Wed, 04 Nov 2026 09:02:00",
        f"Forwarding in case this didn't reach payroll.\n\n---------- Forwarded message ---------\n{bonus_body}",
        [adj("bonus", N["E06"], bonus_amount=1500)])
    put("inbox", "06_hours.eml", mgr, f"Timesheet fix {first['E07']}", "Thu, 05 Nov 2026 14:20:00",
        f"{first['E07']} was short 6 hours on the last timesheet (Oct 27 shift). Please add 6 hours to this pay.\n",
        [adj("hours_correction", N["E07"], extra_hours=6)])

    # ----- extra labelled emails for the eval (not applied to this pay run)
    nh2, nh3 = extra_new[0], extra_new[1]
    ev = [
        ("01_pct_raise.eml", mgr, "Merit increase", f"{N['E03']} gets a 4% merit increase from Nov 1.",
         [adj("raise", N["E03"], effective_date="2026-11-01")]),
        ("02_hourly_raise.eml", mgr, "Rate change", f"Bump {first['E08']} to $26.25/hr starting November 2.",
         [adj("raise", N["E08"], effective_date="2026-11-02", new_hourly_rate=26.25)]),
        ("03_cancelled.eml", mgr, "Re: bonus", f"Please disregard my earlier note about {first['E02']}'s bonus. "
         "We're moving it to the December run.", []),
        ("04_question.eml", f"{first['E05'].lower()}@{DOMAIN}", "Vacation balance",
         f"Quick question: how many vacation days does {first['E05']} have left this year?", []),
        ("05_multi.eml", mgr, "Two things for this run",
         f"Two things for this run: {N['E03']} has a $750 bonus for the safety award.\n"
         f"And {N['E07']} needs 3.5 extra hours added for the Saturday inventory count.",
         [adj("bonus", N["E03"], bonus_amount=750), adj("hours_correction", N["E07"], extra_hours=3.5)]),
        ("06_leave_words.eml", mgr, "Time off", f"{first['E01']} will be off unpaid Wednesday to Friday (Nov 11-13).",
         [adj("unpaid_leave", N["E01"], effective_date="2026-11-11", unpaid_days=3)]),
        ("07_new_hourly.eml", hr, "New warehouse hire",
         f"{nh2} joins us Monday 2026-11-16 at $23.00 an hour. SIN and TD1 received.",
         [adj("new_hire", nh2, effective_date="2026-11-16", new_hourly_rate=23.0, sin_provided=True, td1_provided=True)]),
        ("08_bonus_k.eml", mgr, "Retention", f"Approved: retention bonus of $2.5k for {N['E05']}.",
         [adj("bonus", N["E05"], bonus_amount=2500)]),
        ("09_raise_k.eml", mgr, "Salary update", f"{first['E06']}'s salary is going to $91k as of November 1st.",
         [adj("raise", N["E06"], effective_date="2026-11-01", new_annual_salary=91000)]),
        ("10_half_day.eml", mgr, "Half day", f"{N['E02']} took a half day unpaid on Nov 3.",
         [adj("unpaid_leave", N["E02"], effective_date="2026-11-03", unpaid_days=0.5)]),
        ("11_remove_hours.eml", mgr, "Overpayment",
         f"{first['E08']} was overpaid 2 hours last period; please deduct 2 hours this time.",
         [adj("hours_correction", N["E08"], extra_hours=-2)]),
        ("12_fyi.eml", hr, "FYI", f"FYI {N['E04']} is moving to the Burlington site next month; no change to pay.", []),
        ("13_new_salaried.eml", hr, "Offer accepted",
         f"Offer accepted! {nh3} starts on November 23 at $70,000. We have the SIN; TD1 forms are pending.",
         [adj("new_hire", nh3, effective_date="2026-11-23", new_annual_salary=70000, sin_provided=True, td1_provided=False)]),
        ("14_raise_and_bonus.eml", mgr, f"{first['E04']} - comp",
         f"{first['E04']}: raise to $88,500 effective 11-01 and a $1,000 anniversary bonus.",
         [adj("raise", N["E04"], effective_date="2026-11-01", new_annual_salary=88500),
          adj("bonus", N["E04"], bonus_amount=1000)]),
        ("15_moves_rate.eml", mgr, "Lead hand", f"Starting the 9th, {first['E07']} moves to $33 an hour as lead hand.",
         [adj("raise", N["E07"], effective_date="2026-11-09", new_hourly_rate=33.0)]),
    ]
    for i, (fname, sender, subject, body, expected) in enumerate(ev):
        put("eval", fname, sender, subject, f"Mon, {2 + i % 5:02d} Nov 2026 09:{i:02d}:00", body + "\n", expected)

    (OUT / "labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    print(f"wrote {len(ROSTER)} employees, {len(labels)} labelled emails to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
