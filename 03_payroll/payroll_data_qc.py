"""Generates the synthetic Montreal payroll dataset (Faker fr_CA, seed 42).

Writes to data/synthetic/payroll_qc/:
  company.yaml        Montreal employer, pay period, pay date (same period as the Ontario set)
  employees.csv       roster with TD1 and TP-1015.3-V claims, synthetic banking, addresses, YTD
  timesheet.csv       hours for hourly staff this period
  hours_history.csv   hours for hourly staff in every other period of 2026 (for year-end and ROE)
  inbox/*.eml         this period's change-request emails
  labels.json         hand-written expected adjustments for the inbox emails

Everything is fictitious: SINs start with 0 (never issued to individuals), institution
999, postal codes in the unused H0A FSA, .test email domains, synthetic CRA and Revenu
Québec account numbers.

Year-to-date amounts come from replaying periods 1-20 through the pay-run engine
(payroll_history.replay_year), so they are exactly what earlier runs would have deducted.

Run: uv run python 03_payroll/payroll_data_qc.py
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

from payroll import load_inputs  # noqa: E402
from shared.mail import build_eml  # noqa: E402
from payroll_data import adj, luhn_sin  # noqa: E402
from payroll_history import replay_year, semi_monthly_periods, ytd_before  # noqa: E402

OUT = ROOT / "data" / "synthetic" / "payroll_qc"
DOMAIN = "atelier-sample.test"
CURRENT = 21

# id, pay_type, salary or hourly rate, start, end, TD1 federal claim, TP-1015.3-V claim (blank = basic), occupation
ROSTER = [
    ("Q01", "salary", "72000", "2022-03-07", "", "", "", "Comptabilité"),      # hand-checked case
    ("Q02", "salary", "120000", "2019-05-13", "", "", "", "Gestion des opérations"),  # over QPP/EI max; QPIP max this period
    ("Q03", "salary", "88000", "2021-09-20", "", "", "", "Gestion de projets"),        # reaches QPP max, QPP2 starts this period
    ("Q04", "hourly", "29.00", "2023-02-06", "", "", "", "Soutien technique"),              # overtime; raise from Nov 9
    ("Q05", "salary", "65000", "2024-08-12", "", "20000", "22000", "Coordination"),  # claims above basic; $2,000 bonus
    ("Q06", "hourly", "26.50", "2026-01-12", "2026-10-23", "", "", "Entrepôt"),  # laid off: ROE
]


def write_employees(rows: list[dict]) -> None:
    cols = ["id", "name", "email", "pay_type", "annual_salary", "hourly_rate", "start_date", "end_date", "province",
            "sin", "td1_received", "td1_federal_claim", "td1_ontario_claim", "tp1015_3_claim", "occupation",
            "address", "city", "postal_code", "institution", "transit", "account",
            "ytd_gross", "ytd_pensionable", "ytd_insurable", "ytd_cpp", "ytd_cpp2", "ytd_ei", "ytd_qpip",
            "ytd_employer_qpip", "ytd_tax"]
    with (OUT / "employees.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    fake = Faker("fr_CA")
    Faker.seed(42)
    rng = random.Random(42)

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "inbox").mkdir(parents=True)

    names: list[str] = []
    while len(names) < len(ROSTER):
        n = f"{fake.first_name()} {fake.last_name()}"
        if " " in n.split(" ", 1)[1] or "-" in n:
            continue
        if n.split()[0] not in {x.split()[0] for x in names} and n.split()[1] not in {x.split()[1] for x in names}:
            names.append(n)
    N = {r[0]: names[i] for i, r in enumerate(ROSTER)}
    first = {k: v.split()[0] for k, v in N.items()}

    (OUT / "company.yaml").write_text(
        "# Synthetic Montreal employer and pay period. Not a real business.\n"
        "company:\n"
        "  name: Atelier Exemple Montréal Inc.\n"
        "  short_name: ATELIER EXEMPLE\n"
        "  payroll_account: 000000000RP0002\n"
        "  rq_id: 0000000000RS0001\n"
        "  originator_id: SAMPLE0002\n"
        "  address: 200 rue Exemple, Montréal QC H0A 0A0\n"
        "  province: QC\n"
        "  street: 200 rue Exemple\n"
        "  city: Montréal\n"
        "  postal_code: H0A 0A0\n"
        "  contact_name: Paie Exemple\n"
        "  contact_phone: 514-555-0100\n"
        f"  contact_email: paie@{DOMAIN}\n"
        "period:\n"
        "  start: '2026-11-01'\n"
        "  end: '2026-11-15'\n"
        "  pay_date: '2026-11-13'\n"
        "  periods_per_year: 24\n"
        f"  number: {CURRENT}\n"
    )

    rows = []
    for (eid, ptype, amount, start, end, fed, qc, occupation) in ROSTER:
        name = N[eid]
        email = (name.lower().replace(" ", ".").replace("'", "")
                 .translate(str.maketrans("éèêëàâîïôöûüç", "eeeeaaiioouuc")) + f"@{DOMAIN}")
        rows.append({
            "id": eid, "name": name, "email": email, "pay_type": ptype,
            "annual_salary": amount if ptype == "salary" else "", "hourly_rate": amount if ptype == "hourly" else "",
            "start_date": start, "end_date": end, "province": "QC", "sin": luhn_sin(rng), "td1_received": "yes",
            "td1_federal_claim": fed, "td1_ontario_claim": "", "tp1015_3_claim": qc, "occupation": occupation,
            "address": f"{rng.randint(100, 9999)} rue de l'Exemple", "city": "Montréal",
            "postal_code": f"H0A 0A{rng.randint(1, 9)}",
            "institution": "999", "transit": f"{rng.randint(10000, 99999)}", "account": f"{rng.randint(1000000, 9999999)}",
            **{k: "0" for k in ["ytd_gross", "ytd_pensionable", "ytd_insurable", "ytd_cpp", "ytd_cpp2", "ytd_ei",
                                "ytd_qpip", "ytd_employer_qpip", "ytd_tax"]},
        })
    write_employees(rows)

    with (OUT / "timesheet.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee_id", "regular_hours", "overtime_hours"])
        w.writerow(["Q04", "80", "6"])

    # Hours for hourly staff in every period except the current one (which uses timesheet.csv).
    periods = semi_monthly_periods(2026)
    with (OUT / "hours_history.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee_id", "period", "regular_hours", "overtime_hours"])
        for per in periods:
            if per.number == CURRENT:
                continue
            w.writerow(["Q04", per.number, 80, rng.choice([0, 0, 0, 2, 4])])
            if per.number == 1:
                w.writerow(["Q06", 1, 30, 0])              # starts Mon 12 Jan: 4 working days
            elif per.number == 20:
                w.writerow(["Q06", 20, 45, 0])             # 16-23 Oct: 6 working days, last day 23 Oct
            elif per.number < 20:
                w.writerow(["Q06", per.number, rng.choice([60, 64, 68, 72, 76, 80]), rng.choice([0, 0, 3])])

    mgr = f"gestion@{DOMAIN}"
    labels: dict[str, list[dict]] = {}

    def put(fname: str, sender: str, subject: str, day: str, body: str, expected: list[dict]):
        (OUT / "inbox" / fname).write_bytes(
            build_eml(sender=sender, to=f"paie@{DOMAIN}", subject=subject, date=f"{day} -0500", body=body))
        labels[f"inbox/{fname}"] = expected

    put("01_bonus.eml", mgr, f"Bonus - {first['Q05']}", "Tue, 03 Nov 2026 10:05:00",
        f"Please add a one-time bonus of $2,000 for {first['Q05']} on this run. "
        f"Thanks for leading the ERP cut-over.\n",
        [adj("bonus", N["Q05"], bonus_amount=2000)])
    put("02_rate.eml", mgr, "Rate change", "Wed, 04 Nov 2026 14:30:00",
        f"Bump {first['Q04']} to $30.50/hr starting November 9.\n",
        [adj("raise", N["Q04"], effective_date="2026-11-09", new_hourly_rate=30.5)])
    (OUT / "labels.json").write_text(json.dumps(labels, indent=2, ensure_ascii=False) + "\n")

    # Year-to-date through period 20, from the engine itself.
    inputs = load_inputs(OUT)
    runs = replay_year(inputs, [], OUT, through=CURRENT - 1)
    ytd = ytd_before(runs, CURRENT)
    pens = {}
    for pr in runs:
        for l in pr.run.lines:
            pens[l.employee.id] = pens.get(l.employee.id, Decimal("0")) + l.gross
    for r in rows:
        t = ytd.get(r["id"])
        if not t:
            continue
        r.update(ytd_gross=t["gross"], ytd_pensionable=pens[r["id"]], ytd_insurable=pens[r["id"]],
                 ytd_cpp=t["cpp"], ytd_cpp2=t["cpp2"], ytd_ei=t["ei"], ytd_qpip=t["qpip"],
                 ytd_employer_qpip=t["employer_qpip"], ytd_tax=t["tax"])
    write_employees(rows)
    print(f"wrote {len(ROSTER)} employees, {len(labels)} labelled emails to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
