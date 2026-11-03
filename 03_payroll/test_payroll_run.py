"""Pay-run assembly, flags, approvals and the four approved outputs, on the synthetic dataset."""

from datetime import date
from decimal import Decimal as Dec

import pytest

from payroll import DATA, build_pay_run, load_inputs, propose, sin_valid, totals, working_days
from payroll_extract import RulesExtractor
from payroll_outputs import (RECORD_LEN, cpa005, journal_lines, julian, ledger_balance, pd7a_summary,
                             post_journal, remittance_due_date, write_payslip)
from shared import ledger
from shared.approvals import ApprovalQueue
from shared.mail import read_mailbox


@pytest.fixture(scope="module")
def run():
    inputs = load_inputs(DATA)
    roster = [e.name for e in inputs.employees]
    adjs = []
    for m in read_mailbox(DATA / "inbox"):
        for a in RulesExtractor().extract(m, roster=roster, period_start=inputs.period.start):
            a["source"] = m.path.name
            adjs.append(a)
    return build_pay_run(inputs, adjs)


def line(run, eid):
    return next(l for l in run.lines if l.employee.id == eid)


def test_period_has_ten_working_days():
    assert len(working_days(date(2026, 11, 1), date(2026, 11, 15))) == 10


def test_sin_check_digit():
    assert sin_valid("046-454-286")       # passes the Luhn check digit
    assert not sin_valid("046-454-287")
    assert not sin_valid("")


def test_raise_applies_from_nov_1(run):
    assert line(run, "E01").gross == Dec("3250.00")          # 78,000 / 24


def test_unpaid_leave_two_days_is_exactly_20_percent_not_flagged(run):
    l = line(run, "E04")
    assert l.gross == Dec("2666.67")                         # 80,000/24 x 8/10
    assert not any("gross changes" in f for f in l.flags)
    assert any("EI annual maximum" in f for f in l.flags)


def test_bonus_flagged_and_duplicate_counted_once(run):
    l = line(run, "E06")
    assert l.bonus == Dec("1500.00")
    assert any("duplicate bonus" in f for f in l.flags)
    assert any("gross changes +38%" in f for f in l.flags)


def test_cpp_max_flag(run):
    l = line(run, "E03")
    assert l.deductions.cpp == Dec("189.45")
    assert any("CPP annual maximum" in f for f in l.flags)


def test_over_max_employee_pays_no_cpp_or_ei_and_is_not_flagged(run):
    l = line(run, "E02")
    assert (l.deductions.cpp, l.deductions.cpp2, l.deductions.ei) == (0, 0, 0)
    assert l.flags == []


def test_new_hire_prorated_and_flagged(run):
    l = line(run, "E09")
    assert l.gross == Dec("1291.67")                         # 62,000/24 x 5/10
    assert any("SIN" in f for f in l.flags) and any("TD1" in f for f in l.flags)


def test_hourly_with_overtime_and_hours_correction(run):
    assert line(run, "E07").gross == Dec("2666.00")          # (80 + 6) x 31.00
    assert line(run, "E08").gross == Dec("2009.00")          # 76 x 24.50 + 4 x 24.50 x 1.5


def test_auto_approve_holds_flagged_lines(run):
    q = ApprovalQueue()
    propose(run, q)
    approved = q.review(auto=True)
    held = {p.payload["employee_id"] for p in q.items if p.decision == "held"}
    assert held == {"E03", "E04", "E06", "E09"}
    assert len(approved) == 5


def test_unknown_employee_becomes_flagged_exception():
    inputs = load_inputs(DATA)
    r = build_pay_run(inputs, [{"type": "bonus", "employee_name": "Nobody Here", "bonus_amount": 100, "source": "x.eml"}])
    assert r.exceptions and r.exceptions[0]["reason"].startswith("employee not found")
    q = ApprovalQueue()
    propose(r, q)
    assert q.items[-1].kind == "pay_exception" and q.items[-1].flags


def test_raise_after_period_not_applied():
    inputs = load_inputs(DATA)
    r = build_pay_run(inputs, [{"type": "raise", "employee_name": inputs.employees[0].name,
                                "new_annual_salary": 90000, "effective_date": "2026-12-01", "source": "x"}])
    l = r.lines[0]
    assert l.gross == Dec("3000.00") and any("after this period" in f for f in l.flags)


def test_mid_period_raise_prorated_by_working_day():
    inputs = load_inputs(DATA)
    r = build_pay_run(inputs, [{"type": "raise", "employee_name": inputs.employees[0].name,
                                "new_annual_salary": 84000, "effective_date": "2026-11-09", "source": "x"}])
    # 5 days at 72,000 and 5 days at 84,000: (3,000 + 3,500) / 2
    assert r.lines[0].gross == Dec("3250.00")


def test_remittance_due_date():
    assert remittance_due_date(date(2026, 11, 13)) == (date(2026, 12, 15), date(2026, 12, 15))
    # Pay in July 2026 -> due Aug 15 2026, a Saturday -> on time if received Monday Aug 17.
    assert remittance_due_date(date(2026, 7, 31)) == (date(2026, 8, 15), date(2026, 8, 17))
    assert remittance_due_date(date(2026, 12, 31))[0] == date(2027, 1, 15)


def test_pd7a_reconciles(run):
    s = pd7a_summary(run.lines, run)
    t = totals(run.lines)
    parts = sum(Dec(s[k]) for k in ["cpp_employee", "cpp_employer", "ei_employee", "ei_employer", "income_tax"])
    assert parts == Dec(s["amount_due"]) == t["remittance"]
    assert s["due_date"] == "2026-12-15"


def test_cpa005_layout(run):
    text = cpa005(run.lines, run)
    recs = text.rstrip("\n").split("\n")
    assert all(len(r) == RECORD_LEN for r in recs)
    assert recs[0][0] == "A" and recs[-1][0] == "Z" and all(r[0] == "C" for r in recs[1:-1])
    assert recs[0][20:24] == "TEST"                          # test file creation number
    assert recs[0][55:58] == "CAD"
    assert [int(r[1:10]) for r in recs] == list(range(1, len(recs) + 1))
    amounts = [int(r[27:37]) for r in recs[1:-1]]
    assert sum(amounts) == int(totals(run.lines)["net"] * 100)
    assert int(recs[-1][46:60]) == sum(amounts) and int(recs[-1][60:68]) == len(amounts)
    c = recs[1]
    assert c[24:27] == "200" and c[37:43] == julian(run.inputs.period.pay_date)
    assert c[43:52].startswith("0999")


def test_julian_date():
    assert julian(date(2026, 11, 13)) == "026317"


def test_journal_balances_and_posts(run, tmp_path):
    jl = journal_lines(run.lines, run)
    assert sum(r["amount_cents"] for r in jl) == 0
    con = ledger.connect(tmp_path / "l.db")
    pr = post_journal(con, run.lines, run)
    assert ledger_balance(con, pr) == 0
    assert con.execute("select status from pay_runs where id=?", (pr,)).fetchone()[0] == "approved"


def test_payslip_pdf(run, tmp_path):
    p = write_payslip(line(run, "E05"), run, tmp_path / "slip.pdf")
    assert p.read_bytes()[:4] == b"%PDF"
