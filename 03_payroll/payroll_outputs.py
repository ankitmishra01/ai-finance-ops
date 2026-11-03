"""Artifacts produced after a person approves pay lines.

(a) payslip PDFs, (b) a PD7A-style remittance summary (plus a Revenu Québec remittance
summary when Quebec employees are paid), (c) a SAMPLE CPA Standard 005 credit file,
(d) a balanced pay-run journal entry in shared.ledger. All deterministic.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from payroll import PayLine, PayRun, totals
from payroll_calc import QC, RATES
from shared import ledger

# ---------------------------------------------------------------- (a) payslips


def write_payslip(line: PayLine, run: PayRun, path: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    e, d, per, co = line.employee, line.deductions, run.inputs.period, run.inputs.company
    styles = getSampleStyleSheet()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, title=f"Pay statement {e.name} {per.pay_date}")

    def m(x: Decimal) -> str:
        return f"{x:,.2f}"

    ytd_gross = e.ytd_gross + line.gross
    plan = "QPP" if d.quebec else "CPP"
    rows = [["Earnings", "Current", ""]] + [[k, m(v), ""] for k, v in line.earnings]
    rows += [["Gross pay", m(line.gross), ""]]
    ded = [["Deductions", "Current", "Year to date"],
           [plan, m(d.cpp), m(e.ytd.cpp + d.cpp)],
           [f"{plan}2", m(d.cpp2), m(e.ytd.cpp2 + d.cpp2)],
           ["EI", m(d.ei), m(e.ytd.ei + d.ei)]]
    if d.quebec:
        ded.append(["QPIP", m(d.qpip), m(e.ytd.qpip + d.qpip)])
    ded += [["Federal income tax", m(d.federal_tax), ""],
           ["Quebec income tax" if d.quebec else "Ontario income tax", m(d.provincial_tax), ""],
           ["Total income tax", m(d.income_tax), m(e.ytd_tax + d.income_tax)],
           ["Total deductions", m(d.employee_total), ""],
           ["Net pay", m(line.net), ""],
           ["Gross pay year to date", "", m(ytd_gross)]]
    style = TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("ALIGN", (1, 0), (-1, -1), "RIGHT")])
    acct = f"****{e.account[-3:]}" if e.account else "cheque"
    story = [
        Paragraph(f"{co.name} - pay statement", styles["Title"]),
        Paragraph("SYNTHETIC DATA - sample output, not a real pay statement", styles["Italic"]),
        Spacer(1, 8),
        Paragraph(f"Employee: {e.name} ({e.id})<br/>Pay period: {per.start} to {per.end} "
                  f"(period {per.number} of {per.periods_per_year})<br/>Pay date: {per.pay_date}<br/>"
                  f"Deposit to account {acct}", styles["Normal"]),
        Spacer(1, 10),
        Table(rows, colWidths=[260, 100, 100], style=style),
        Spacer(1, 10),
        Table(ded, colWidths=[260, 100, 100], style=style),
        Spacer(1, 10),
        Paragraph(f"Employer contributions this period: {plan} {m(d.employer_cpp)}, {plan}2 {m(d.employer_cpp2)}, "
                  f"EI {m(d.employer_ei)}"
                  + (f", QPIP {m(d.employer_qpip)}, health services fund {m(d.employer_hsf)}." if d.quebec else "."),
                  styles["Normal"]),
        Paragraph(f"Deductions per CRA {RATES['edition']}"
                  + (f" and Revenu Québec {QC['edition'].split(' and ')[0]}." if d.quebec else "."), styles["Normal"]),
    ]
    doc.build(story)
    return path


# ---------------------------------------------------------------- (b) PD7A


def remittance_due_date(pay_date: date) -> tuple[date, date]:
    """Regular remitter: 15th of the month after the month remuneration was paid.
    Returns (statutory due date, date it must reach the CRA if that falls on a weekend).
    CRA public holidays are not modelled; check the CRA page for the year."""
    y, mth = (pay_date.year + 1, 1) if pay_date.month == 12 else (pay_date.year, pay_date.month + 1)
    due = date(y, mth, RATES["remittance"]["due_day_of_following_month"])
    effective = due
    while effective.weekday() >= 5:
        effective += timedelta(days=1)
    return due, effective


def pd7a_summary(lines: list[PayLine], run: PayRun) -> dict:
    """CRA remittance. For Quebec employees only federal tax and EI go to the CRA; QPP,
    QPIP and Quebec tax go to Revenu Québec (rq_summary)."""
    t = totals(lines)
    cra = [l for l in lines if not l.deductions.quebec]
    qc = [l for l in lines if l.deductions.quebec]
    tc, tq = totals(cra), totals(qc)
    due, effective = remittance_due_date(run.inputs.period.pay_date)
    cpp_total = tc["cpp"] + tc["employer_cpp"] + tc["cpp2"] + tc["employer_cpp2"]
    ei_total = t["ei"] + t["employer_ei"]
    income_tax = tc["income_tax"] + tq["federal_tax"]
    amount = cpp_total + ei_total + income_tax
    assert amount == t["cra_remittance"], "remittance does not reconcile to pay lines"
    return {
        "label": "PD7A-style summary (SYNTHETIC; not a CRA form)",
        "payroll_account": run.inputs.company.payroll_account,
        "remitter_type": RATES["remittance"]["remitter_type"],
        "pay_date": run.inputs.period.pay_date.isoformat(),
        "remuneration_period": run.inputs.period.pay_date.strftime("%Y-%m"),
        "gross_payroll": str(t["gross"]),
        "employees_paid": len(lines),
        "cpp_employee": str(tc["cpp"] + tc["cpp2"]),
        "cpp_employer": str(tc["employer_cpp"] + tc["employer_cpp2"]),
        "ei_employee": str(t["ei"]),
        "ei_employer": str(t["employer_ei"]),
        "income_tax": str(income_tax),
        "amount_due": str(amount),
        "due_date": due.isoformat(),
        "due_date_note": ("15th of the month after the pay date" if due == effective
                          else f"15th falls on a weekend; payment received by {effective} is on time"),
        "source": RATES["sources"]["remittance_due_dates"],
    }


def write_pd7a(summary: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pd7a_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    s = summary
    text = f"""{s['label']}
Payroll account:            {s['payroll_account']}
Remuneration paid in:       {s['remuneration_period']} (pay date {s['pay_date']})
Gross payroll:              {Decimal(s['gross_payroll']):>12,.2f}
Employees paid:             {s['employees_paid']:>12}
CPP + CPP2, employee:       {Decimal(s['cpp_employee']):>12,.2f}
CPP + CPP2, employer:       {Decimal(s['cpp_employer']):>12,.2f}
EI, employee:               {Decimal(s['ei_employee']):>12,.2f}
EI, employer (1.4x):        {Decimal(s['ei_employer']):>12,.2f}
Income tax:                 {Decimal(s['income_tax']):>12,.2f}
AMOUNT DUE TO CRA:          {Decimal(s['amount_due']):>12,.2f}
Due date:                   {s['due_date']} ({s['due_date_note']})
"""
    p = folder / "pd7a_summary.txt"
    p.write_text(text)
    return p


def rq_summary(lines: list[PayLine], run: PayRun) -> dict | None:
    """Revenu Québec remittance summary for Quebec lines: QPP, QPP2, QPIP (employee and
    employer), Quebec income tax and the health services fund. None if no Quebec lines.
    The due date is not computed: Revenu Québec's remittance schedule was not verified for
    this build (see README)."""
    qc = [l for l in lines if l.deductions.quebec]
    if not qc:
        return None
    t = totals(qc)
    parts = {
        "qpp_employee": t["cpp"] + t["cpp2"],
        "qpp_employer": t["employer_cpp"] + t["employer_cpp2"],
        "qpip_employee": t["qpip"],
        "qpip_employer": t["employer_qpip"],
        "quebec_income_tax": t["provincial_tax"],
        "health_services_fund": t["employer_hsf"],
    }
    amount = sum(parts.values(), Decimal("0"))
    assert amount == t["rq_remittance"], "Revenu Québec remittance does not reconcile to pay lines"
    return {
        "label": "Revenu Québec remittance summary (SYNTHETIC; not a TPZ-1015.R form)",
        "rq_id": run.inputs.company.rq_id,
        "pay_date": run.inputs.period.pay_date.isoformat(),
        "remuneration_period": run.inputs.period.pay_date.strftime("%Y-%m"),
        "quebec_gross_payroll": str(t["gross"]),
        "employees_paid": len(qc),
        **{k: str(v) for k, v in parts.items()},
        "amount_due": str(amount),
        "due_date": None,
        "due_date_note": "not computed: check your Revenu Québec remittance frequency and schedule",
        "health_services_fund_rate": str(QC["health_services_fund"]["rate_total_payroll_1m_or_less"]),
        "source": QC["sources"]["tp1015fv_2026_01_pdf"],
    }


def write_rq_summary(summary: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rq_remittance_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    s = summary
    text = f"""{s['label']}
RQ identification number:   {s['rq_id']}
Remuneration paid in:       {s['remuneration_period']} (pay date {s['pay_date']})
Quebec gross payroll:       {Decimal(s['quebec_gross_payroll']):>12,.2f}
Employees paid:             {s['employees_paid']:>12}
QPP + QPP2, employee:       {Decimal(s['qpp_employee']):>12,.2f}
QPP + QPP2, employer:       {Decimal(s['qpp_employer']):>12,.2f}
QPIP, employee:             {Decimal(s['qpip_employee']):>12,.2f}
QPIP, employer:             {Decimal(s['qpip_employer']):>12,.2f}
Quebec income tax:          {Decimal(s['quebec_income_tax']):>12,.2f}
Health services fund:       {Decimal(s['health_services_fund']):>12,.2f}  (rate {Decimal(s['health_services_fund_rate']) * 100:.2f}%)
AMOUNT DUE TO REVENU QUÉBEC:{Decimal(s['amount_due']):>12,.2f}
Due date:                   {s['due_date_note']}
"""
    p = folder / "rq_remittance_summary.txt"
    p.write_text(text)
    return p


# ---------------------------------------------------------------- (c) CPA 005

RECORD_LEN = 1464
SEGMENT_LEN = 240
TXN_PAYROLL = "200"   # CPA transaction code: payroll deposit


def julian(d: date) -> str:
    """CPA 005 date format 0YYDDD."""
    return f"0{d.year % 100:02d}{d.timetuple().tm_yday:03d}"


def _an(v: str, n: int) -> str:
    v = (v or "").upper()[:n]
    return v.ljust(n)


def _n(v: int, n: int) -> str:
    s = str(int(v))
    assert len(s) <= n, f"{v} does not fit in {n} digits"
    return s.zfill(n)


def cpa005(lines: list[PayLine], run: PayRun, *, file_creation_number: str = "TEST",
           created: date | None = None, processing_centre: str = "00320") -> str:
    """One logical file: header A, one C record per payee (segment one used), trailer Z.

    Layout follows the CPA 005 credit file as published by Canadian banks (1464-byte
    records). SAMPLE ONLY: the originator ID and accounts are synthetic, and the file
    creation number is TEST, which banks treat as a test file.
    """
    co, per = run.inputs.company, run.inputs.period
    created = created or per.pay_date
    orig = _an(co.originator_id, 10)
    fcn = _an(file_creation_number, 4)
    recs = []
    hdr = "A" + _n(1, 9) + orig + fcn + julian(created) + _n(int(processing_centre), 5) + " " * 20 + "CAD"
    recs.append(hdr.ljust(RECORD_LEN))
    total_cents = 0
    payees = [l for l in lines if l.employee.account]
    for i, l in enumerate(payees, start=2):
        e = l.employee
        cents = int((l.net * 100).to_integral_value())
        total_cents += cents
        seg = (
            TXN_PAYROLL
            + _n(cents, 10)
            + julian(per.pay_date)
            + _n(int("0" + e.institution.zfill(3) + e.transit.zfill(5)), 9)
            + _an(e.account, 12)
            + "0" * 22                      # reserved / item trace, zero fill
            + "0" * 3                       # reserved, zero fill
            + _an(co.short_name, 15)
            + _an(e.name, 30)
            + _an(co.name, 30)
            + orig
            + _an(f"{e.id}-{per.pay_date:%Y%m%d}", 19)
            + "0" * 9                       # reserved, zero fill
            + " " * 12                      # reserved, blank
            + _an("PAYROLL SAMPLE", 15)
            + " " * 22 + " " * 2 + " " * 11  # reserved, blank
        )
        assert len(seg) == SEGMENT_LEN, len(seg)
        rec = "C" + _n(i, 9) + orig + fcn + seg
        recs.append(rec.ljust(RECORD_LEN))
    trl = ("Z" + _n(len(payees) + 2, 9) + orig + fcn + "0" * 14 + "0" * 8
           + _n(total_cents, 14) + _n(len(payees), 8))
    recs.append(trl.ljust(RECORD_LEN, "0"))
    for r in recs:
        assert len(r) == RECORD_LEN
    return "\n".join(recs) + "\n"


def write_cpa005(lines: list[PayLine], run: PayRun, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "SAMPLE_NOT_FOR_UPLOAD_cpa005.txt"
    p.write_text(cpa005(lines, run))
    (folder / "SAMPLE_NOT_FOR_UPLOAD_README.txt").write_text(
        "SAMPLE ONLY - NOT FOR UPLOAD.\n"
        "Synthetic CPA Standard 005 credit file generated from synthetic payroll data.\n"
        "File creation number is TEST. Originator ID, institution, transit and account numbers are fictitious.\n"
        "Confirm the layout against your own financial institution's CPA 005 specification before any real use.\n")
    return p


# ---------------------------------------------------------------- (d) journal

ACCOUNTS = {
    "wages": "5100 Wages and salaries",
    "er_cpp": "5110 Employer CPP expense",
    "er_ei": "5120 Employer EI expense",
    "er_qpp": "5115 Employer QPP expense",
    "er_qpip": "5125 Employer QPIP expense",
    "er_hsf": "5130 Health services fund expense",
    "cra": "2310 Source deductions payable - CRA",
    "rq": "2315 Source deductions payable - Revenu Quebec",
    "net": "2300 Net pay payable",
}


def journal_lines(lines: list[PayLine], run: PayRun) -> list[dict]:
    t = totals(lines)
    on = totals([l for l in lines if not l.deductions.quebec])
    qc = totals([l for l in lines if l.deductions.quebec])
    cents = lambda x: int((x * 100).to_integral_value())  # noqa: E731
    rows = [
        (ACCOUNTS["wages"], cents(t["gross"])),
        (ACCOUNTS["er_cpp"], cents(on["employer_cpp"] + on["employer_cpp2"])),
        (ACCOUNTS["er_ei"], cents(t["employer_ei"])),
    ]
    if qc["gross"]:
        rows += [
            (ACCOUNTS["er_qpp"], cents(qc["employer_cpp"] + qc["employer_cpp2"])),
            (ACCOUNTS["er_qpip"], cents(qc["employer_qpip"])),
            (ACCOUNTS["er_hsf"], cents(qc["employer_hsf"])),
        ]
    rows.append((ACCOUNTS["cra"], -cents(t["cra_remittance"])))
    if qc["gross"]:
        rows.append((ACCOUNTS["rq"], -cents(t["rq_remittance"])))
    rows.append((ACCOUNTS["net"], -cents(t["net"])))
    assert sum(a for _, a in rows) == 0, "pay-run journal does not balance"
    return [{"account": a, "amount_cents": c} for a, c in rows]


def post_journal(con, lines: list[PayLine], run: PayRun) -> int:
    per = run.inputs.period
    t = totals(lines)
    jl = journal_lines(lines, run)
    pr_id = con.execute(
        "insert into pay_runs (period_start, period_end, pay_date, status, summary_json) values (?, ?, ?, ?, ?)",
        (per.start.isoformat(), per.end.isoformat(), per.pay_date.isoformat(), "approved",
         json.dumps({k: str(v) for k, v in t.items()} | {"employees": [l.employee.id for l in lines]})),
    ).lastrowid
    for row in jl:
        ledger.add_transaction(con, date=per.pay_date.isoformat(), description=f"Pay run {pr_id} {per.start}..{per.end}",
                               amount_cents=row["amount_cents"], account=row["account"], category="payroll",
                               tax_code="", evidence_path="")
    con.commit()
    return pr_id


def ledger_balance(con, pay_run_id: int) -> int:
    return con.execute("select coalesce(sum(amount_cents), 0) from transactions where description like ?",
                       (f"Pay run {pay_run_id} %",)).fetchone()[0] or 0

