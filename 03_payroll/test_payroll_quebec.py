"""Quebec deductions, the Montreal pay run, year-end slips and the ROE.

The Quebec figures are checked two ways:
1. Against the worked examples Revenu Québec publishes in TP-1015.F-V (2026-01),
   appendices 1, 2 and 3, reproduced here to the cent.
2. One employee worked by hand from the formulas (test_hand_checked_quebec_72000).
They were not checked against WebRAS or PDOC: WebRAS returned HTTP 403 to automated
requests and PDOC is a JavaScript application that a plain fetch cannot drive.
"""

import xml.etree.ElementTree as ET
from decimal import Decimal as Dec

import pytest

from payroll import build_pay_run, load_inputs, propose
from payroll_calc import (QC, YTD, PeriodInput, _qc_bracket, compute, ei_premium_qc, federal_tax_qc,
                          qpip_employer_premium, qpip_premium, qpp2_contribution, qpp_contribution,
                          quebec_tax_lump_sum, quebec_tax_regular)
from payroll_extract import RulesExtractor
from payroll_history import replay_year, semi_monthly_periods, ytd_before
from payroll_outputs import journal_lines, pd7a_summary, rq_summary
from payroll_roe import roe_for
from payroll_yearend import rl1_slips, rl1_totals, t4_slips, t4_summary, t4_xml
from shared.approvals import ApprovalQueue
from shared.mail import read_mailbox

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QC_DATA = ROOT / "data" / "synthetic" / "payroll_qc"
ON_DATA = ROOT / "data" / "synthetic" / "payroll"


# ---------------------------------------------------------------- rates

def test_quebec_rates_file_is_2026_and_sourced():
    assert QC["year"] == 2026
    assert QC["qpp"]["rate"] == Dec("0.0630") and QC["qpp"]["max_contribution"] == Dec("4479.30")
    assert QC["qpp2"]["max_contribution"] == Dec("416.00") and QC["qpp2"]["yampe"] == Dec("85000")
    assert QC["qpip"]["max_premium"] == Dec("442.90") and QC["qpip"]["max_insurable_earnings"] == Dec("103000")
    assert QC["ei"]["rate"] == Dec("0.0130") and QC["ei"]["max_premium"] == Dec("895.70")
    assert QC["federal"]["abatement"] == Dec("0.165")
    assert QC["quebec"]["basic_personal_amount"] == Dec("18952")
    # Internal consistency of the published maximums.
    assert (QC["qpp"]["ympe"] - QC["qpp"]["basic_exemption"]) * QC["qpp"]["rate"] == QC["qpp"]["max_contribution"]
    assert (QC["qpp"]["ympe"] - QC["qpp"]["basic_exemption"]) * QC["qpp"]["base_rate"] == QC["qpp"]["max_base_contribution"]
    assert QC["qpip"]["max_insurable_earnings"] * QC["qpip"]["rate"] == QC["qpip"]["max_premium"]
    assert QC["ei"]["max_insurable_earnings"] * QC["ei"]["rate"] == QC["ei"]["max_premium"]
    for url in QC["sources"].values():
        assert url.startswith(("https://www.canada.ca/", "https://www.revenuquebec.ca/"))


# ---------------------------------------------------------------- hand-checked employee

def test_hand_checked_quebec_72000():
    """$72,000/yr in Montreal, semi-monthly (P=24), basic TD1 and TP-1015.3-V claims, no YTD.

    I (gross) = 3,000.00
    QPP  C  = 0.0630 x (3,000 - 145.83) = 179.8127 -> 179.81     (3,500/24 truncated to 145.83)
    QPP2 C2 = 0.04 x (0 + 3,000 - 74,600) < 0 -> 0
    EI      = 0.0130 x 3,000 = 39.00 ; employer 1.4 x 39.00 = 54.60
    QPIP    = 0.00430 x 3,000 = 12.90 ; employer 0.00602 x 3,000 = 18.06
    Federal (T4127, Quebec):
      F5Q = 179.81 x 0.0100/0.0630 = 28.5413 ; A = 24 x (3,000 - 28.5413) = 71,315.01
      R = 0.205, K = 3,804 ; K1 = 0.14 x 16,452 = 2,303.28
      K2Q = 0.14 x (24 x 179.81 x 0.0530/0.0630 = 3,630.45)  = 508.26
          + 0.14 x min(24 x 39.00 = 936.00, 895.70)          = 125.40
          + 0.14 x (24 x 3,000 x 0.0043 = 309.60)             =  43.34   -> 677.00
      K4 = 0.14 x 1,501 = 210.14
      T3 = 0.205 x 71,315.01 - 3,804 - 2,303.28 - 677.00 - 210.14 = 7,625.15
      T1 = T3 - 0.165 x T3 = 6,367.00 ; /24 = 265.29
    Quebec (TP-1015.F-V 2.1.1):
      H   = min(0.06 x 3,000 = 180, 1,450/24 = 60.42) = 60.42
      CSA = 179.81 x 0.01/0.0630 + 0 = 28.54
      I   = 24 x (3,000 - 0 - 60.42 - 28.54) = 69,864.96 -> T = 19%, K = 2,717
      Y   = 0.19 x 69,864.96 - 2,717 - 0.14 x 18,952 = 13,274.34 - 2,717 - 2,653.28 = 7,904.06
      A   = 7,904.06 / 24 = 329.34
    Net = 3,000 - 179.81 - 39.00 - 12.90 - 265.29 - 329.34 = 2,173.66
    """
    d = compute(PeriodInput(24, Dec("3000.00"), province="QC"))
    assert (d.cpp, d.cpp2, d.ei, d.qpip) == (Dec("179.81"), Dec("0.00"), Dec("39.00"), Dec("12.90"))
    assert (d.employer_cpp, d.employer_ei, d.employer_qpip) == (Dec("179.81"), Dec("54.60"), Dec("18.06"))
    assert d.federal_tax == Dec("265.29")
    assert d.provincial_tax == Dec("329.34")
    assert Dec("3000.00") - d.employee_total == Dec("2173.66")
    fed = d.detail["federal_without_bonus"]
    assert round(fed["K2Q"], 2) == Dec("677.00") and round(fed["T3"], 2) == Dec("7625.15")
    q = d.detail["quebec_regular"]
    assert (q["H"], q["CSA"], q["I"]) == (Dec("60.42"), Dec("28.54"), Dec("69864.96"))
    assert d.ontario_tax == 0


def test_same_salary_quebec_vs_ontario_differs_as_expected():
    on = compute(PeriodInput(24, Dec("3000.00")))
    qc = compute(PeriodInput(24, Dec("3000.00"), province="QC"))
    assert qc.cpp > on.cpp                       # 6.30% vs 5.95%
    assert qc.ei < on.ei                         # 1.30% vs 1.63%
    assert qc.federal_tax < on.federal_tax       # 16.5% abatement
    assert on.qpip == 0 and qc.qpip > 0


# ---------------------------------------------------------------- Revenu Québec worked examples

@pytest.mark.parametrize("periods, C, C2, Q, Q1, csa, I, A", [
    # TP-1015.F-V (2026-01) appendix 1: $4,000 biweekly, RPP $200, line 10 = $21,830,
    # $100 FTQ and $150 Fondaction shares for the first 20 periods.
    ("1-18", "243.52", "0", "100", "150", "38.65", "96345.08", "444.51"),
    ("20-21", "0", "160", "100", "150", "160.00", "93189.98", "421.46"),
    ("22", "0", "40", "0", "0", "40.00", "96309.98", "481.76"),
    ("last 4", "0", "0", "0", "0", "0.00", "97349.98", "489.36"),
])
def test_tp1015fv_appendix_1(periods, C, C2, Q, Q1, csa, I, A):
    r = quebec_tax_regular(P=26, G=Dec("4000"), C=Dec(C), C2=Dec(C2), S3=Dec("4000"), F=Dec("200"),
                           E=Dec("21830"), Q=Dec(Q), Q1=Dec(Q1))
    assert r["H"] == Dec("55.77")
    assert r["CSA"] == Dec(csa)
    assert r["I"] == Dec(I)
    assert r["A"] == Dec(A)


def test_tp1015fv_appendix_2_retroactive_pay_method_2():
    """$1,500 weekly, RPP $100/week; retroactive pay $4,000 with $400 RPP on it.
    Guide: CSA 14.82, CSB 39.51, I = 74,140.09, A = 0.19 x (3,600 - 39.51) = 676.49."""
    C, _ = qpp_contribution(Dec("5500"), 52, Dec("0"))
    assert C == Dec("342.26")
    r = quebec_tax_lump_sum(P=52, G=Dec("1500"), C=C, C2=Dec("0"), S3=Dec("5500"), B2=Dec("4000"),
                            F=Dec("100"), F_lump=Dec("400"))
    assert (r["CSA"], r["CSB"], r["I"], r["A"]) == (Dec("14.82"), Dec("39.51"), Dec("74140.09"), Dec("676.49"))


def test_tp1015fv_appendix_3_qpp_and_qpp2():
    # 19th period: A5 = 4,383.36 after 18 x 243.52 ; S5 = 72,000
    assert qpp_contribution(Dec("4000"), 26, Dec("4383.36")) == (Dec("95.94"), True)
    assert qpp2_contribution(Dec("4000"), Dec("72000"), Dec("0"))[0] == Dec("56.00")
    # 20th: QPP at max ; QPP2 = 0.04 x (76,000 + 4,000 - 76,000)
    assert qpp_contribution(Dec("4000"), 26, Dec("4479.30"))[0] == Dec("0.00")
    assert qpp2_contribution(Dec("4000"), Dec("76000"), Dec("56"))[0] == Dec("160.00")
    # 22nd: 416 - 376 = 40 left
    assert qpp2_contribution(Dec("4000"), Dec("84000"), Dec("376")) == (Dec("40.00"), True)
    # Per-period exemption 3,500/26 = 134.615 truncated to 134.61 -> 0.0630 x 3,865.39 = 243.52
    assert qpp_contribution(Dec("4000"), 26, Dec("0"))[0] == Dec("243.52")


# ---------------------------------------------------------------- QPIP, EI, brackets, lump sums

def test_qpip_caps_and_min_cent():
    assert qpip_premium(Dec("5000"), Dec("430.00")) == (Dec("12.90"), True)
    assert qpip_premium(Dec("5000"), Dec("442.90")) == (Dec("0.00"), False)
    assert qpip_premium(Dec("1.00"), Dec("0"))[0] == Dec("0.01")      # 0.0043 rounds up to one cent
    assert qpip_employer_premium(Dec("5000"), Dec("602.00")) == Dec("18.06")
    assert qpip_employer_premium(Dec("5000"), Dec("620.06")) == Dec("0.00")


def test_ei_quebec_rate_and_cap():
    assert ei_premium_qc(Dec("3000"), Dec("0")) == (Dec("39.00"), False)
    assert ei_premium_qc(Dec("3000"), Dec("880.00")) == (Dec("15.70"), True)


def test_quebec_brackets_are_more_than():
    assert _qc_bracket(Dec("54345")) == (Dec("0.14"), Dec("0"))
    assert _qc_bracket(Dec("54345.01")) == (Dec("0.19"), Dec("2717"))
    assert _qc_bracket(Dec("132245.01"))[0] == Dec("0.2575")


def test_federal_abatement_is_16_5_percent_of_basic_federal_tax():
    t = federal_tax_qc(Dec("80000"), qpp_base_annual=Dec("3768.30"), ei_annual=Dec("895.70"),
                       qpip_base_annual=Dec("344"), gross_annual=Dec("80000"), federal_claim=None)
    assert t["T1"] == t["T3"] * Dec("0.835")


def test_lump_sum_low_income_flat_7_percent():
    r = quebec_tax_lump_sum(P=24, G=Dec("600"), C=Dec("28.62"), C2=Dec("0"), S3=Dec("1600"), B2=Dec("1000"))
    assert r["method"] == "flat 7%" and r["A"] == Dec("70.00")      # 24 x 600 + 1,000 <= 18,952


def test_bonus_quebec_method_2_uses_marginal_rate():
    base = compute(PeriodInput(24, Dec("2708.33"), province="QC", quebec_claim=Dec("22000"), federal_claim=Dec("20000")))
    bonus = compute(PeriodInput(24, Dec("2708.33"), bonus=Dec("2000"), province="QC",
                                quebec_claim=Dec("22000"), federal_claim=Dec("20000")))
    lump = bonus.detail["quebec_lump_sum"]
    assert lump["method"] == "method 2" and lump["T"] == Dec("0.19")
    assert lump["A"] == (Dec("0.19") * (Dec("2000") - lump["CSB"])).quantize(Dec("0.01"))   # 0.19 x 1,980.62
    assert bonus.qpip > base.qpip and bonus.cpp > base.cpp


def test_remittance_split_cra_vs_revenu_quebec():
    d = compute(PeriodInput(24, Dec("3000.00"), province="QC"))
    assert d.cra_remittance == d.ei + d.employer_ei + d.federal_tax
    assert d.rq_remittance == (2 * d.cpp + 2 * d.cpp2 + d.qpip + d.employer_qpip + d.provincial_tax + d.employer_hsf)
    assert d.employer_hsf == Dec("49.50")                              # 1.65% x 3,000
    on = compute(PeriodInput(24, Dec("3000.00")))
    assert on.rq_remittance == 0 and on.cra_remittance == on.remittance


# ---------------------------------------------------------------- Montreal pay run

def _adjs(data):
    inputs = load_inputs(data)
    roster = [e.name for e in inputs.employees]
    out = []
    for m in read_mailbox(data / "inbox"):
        for a in RulesExtractor().extract(m, roster=roster, period_start=inputs.period.start):
            a["source"] = m.path.name
            out.append(a)
    return inputs, out


@pytest.fixture(scope="module")
def qc_run():
    inputs, adjs = _adjs(QC_DATA)
    return build_pay_run(inputs, adjs)


def _line(run, eid):
    return next(l for l in run.lines if l.employee.id == eid)


def test_montreal_run_hand_checked_employee(qc_run):
    l = _line(qc_run, "Q01")
    d = l.deductions
    assert d.quebec and (d.cpp, d.ei, d.qpip, d.federal_tax, d.provincial_tax) == (
        Dec("179.81"), Dec("39.00"), Dec("12.90"), Dec("265.29"), Dec("329.34"))
    assert l.net == Dec("2173.66")


def test_montreal_run_flags(qc_run):
    assert any("QPIP annual maximum" in f for f in _line(qc_run, "Q02").flags)
    assert _line(qc_run, "Q02").deductions.qpip == Dec("12.90")       # 442.90 - 430.00
    q3 = _line(qc_run, "Q03").deductions
    assert q3.cpp == Dec("43.10") and q3.cpp2 == Dec("96.00")         # QPP max reached; QPP2 starts
    assert any("QPP annual maximum" in f for f in _line(qc_run, "Q03").flags)
    assert _line(qc_run, "Q05").bonus == Dec("2000.00")
    assert all(l.employee.id != "Q06" for l in qc_run.lines)          # laid off in October


def test_montreal_mid_period_hourly_raise(qc_run):
    # 80 h split over 10 days: 5 days at 29.00, 5 at 30.50 ; 6 OT hours at 30.50 x 1.5
    assert _line(qc_run, "Q04").gross == Dec("2654.50")


def test_montreal_remittances_and_journal(qc_run):
    q = ApprovalQueue()
    propose(qc_run, q)
    assert all(p.kind == "pay_line" for p in q.items)
    pd7a = pd7a_summary(qc_run.lines, qc_run)
    rq = rq_summary(qc_run.lines, qc_run)
    assert Dec(pd7a["cpp_employee"]) == 0                              # QPP never goes to the CRA
    assert Dec(pd7a["amount_due"]) + Dec(rq["amount_due"]) == sum(l.deductions.remittance for l in qc_run.lines)
    assert rq["due_date"] is None                                      # not verified, not invented
    jl = journal_lines(qc_run.lines, qc_run)
    assert sum(r["amount_cents"] for r in jl) == 0
    assert any("Revenu Quebec" in r["account"] for r in jl)


# ---------------------------------------------------------------- history and year-end

@pytest.mark.parametrize("data", [ON_DATA, QC_DATA], ids=["ontario", "montreal"])
def test_replay_reproduces_employee_ytd(data):
    inputs = load_inputs(data)
    runs = replay_year(inputs, [], data, through=inputs.period.number)
    ytd = ytd_before(runs, inputs.period.number)
    for e in inputs.employees:
        t = ytd.get(e.id)
        if t is None:
            assert e.ytd_gross == 0
            continue
        assert (t["gross"], t["cpp"], t["cpp2"], t["ei"], t["tax"]) == (
            e.ytd_gross, e.ytd.cpp, e.ytd.cpp2, e.ytd.ei, e.ytd_tax), e.id
        assert t["qpip"] == e.ytd.qpip


def test_semi_monthly_calendar():
    ps = semi_monthly_periods(2026)
    assert len(ps) == 24 and ps[20].number == 21
    assert (ps[20].start.isoformat(), ps[20].end.isoformat(), ps[20].pay_date.isoformat()) == (
        "2026-11-01", "2026-11-15", "2026-11-13")


@pytest.fixture(scope="module")
def qc_year():
    inputs, adjs = _adjs(QC_DATA)
    return inputs, replay_year(inputs, adjs, QC_DATA)


@pytest.fixture(scope="module")
def on_year():
    inputs, adjs = _adjs(ON_DATA)
    return inputs, replay_year(inputs, adjs, ON_DATA)


def test_t4_quebec_boxes(qc_year):
    inputs, runs = qc_year
    slips = {s["employee_id"]: s for s in t4_slips(runs)}
    q2 = slips["Q02"]
    assert "box16_cpp" not in q2 and q2["box17_qpp"] == Dec("4479.30") and q2["box17A_qpp2"] == Dec("416.00")
    assert q2["box24_ei_insurable"] == Dec("68900") and q2["box26_pensionable"] == Dec("85000")
    assert q2["box55_ppip"] == Dec("442.90") and q2["box56_ppip_insurable"] == Dec("103000")
    # Box 22 is federal tax only for Quebec; Quebec tax is on the RL-1.
    fed = sum(l.deductions.federal_tax for pr in runs for l in pr.run.lines if l.employee.id == "Q02")
    assert q2["box22_income_tax"] == fed
    q1 = slips["Q01"]
    assert q1["box14_employment_income"] == Dec("72000.00") and q1["box17_qpp"] == 24 * Dec("179.81")


def test_t4_summary_reconciles(qc_year, on_year):
    for inputs, runs in (qc_year, on_year):
        slips = t4_slips(runs)
        s = t4_summary(runs, slips, inputs.company, 2026)
        # Line 19 is 1.4 x total EI (RC4120); remittances rounded 1.4 x EI each period.
        diff = s["line80_total_deductions"] - s["line82_remittances"]
        assert abs(diff) <= Dec("2.00")
        assert diff == s["line19_employer_ei"] - sum(l.deductions.employer_ei for pr in runs for l in pr.run.lines)
        assert s["line19_employer_ei"] == (s["line18_employees_ei"] * Dec("1.4")).quantize(Dec("0.01"))
        assert s["line14_employment_income"] == sum(l.gross for pr in runs for l in pr.run.lines)
        assert s["line88_slips"] == len(slips)
    s = t4_summary(qc_year[1], t4_slips(qc_year[1]), qc_year[0].company, 2026)
    assert s["line16_employees_cpp"] == 0 and s["line27_employer_cpp"] == 0  # QPP is not a T4 Summary line


def test_t4_xml_structure(qc_year, on_year):
    for inputs, runs in (qc_year, on_year):
        slips = t4_slips(runs)
        summ = t4_summary(runs, slips, inputs.company, 2026)
        xml = t4_xml(slips, summ, inputs.company, 2026, {e.id: e for e in inputs.employees})
        assert "SAMPLE" in xml.splitlines()[1] and "NOT FOR FILING" in xml
        root = ET.fromstring(xml)
        assert root.tag == "Submission" and [c.tag for c in root] == ["T619", "Return"]
        t4 = root.find("Return/T4")
        assert [c.tag for c in t4][-1] == "T4Summary"
        assert len(t4.findall("T4Slip")) == len(slips) == int(t4.findtext("T4Summary/slp_cnt"))
        slip = t4.find("T4Slip")
        order = [c.tag for c in slip]
        assert order.index("sin") < order.index("bn") < order.index("rpt_tcd") < order.index("empt_prov_cd") < order.index("T4_AMT")
        amt_total = sum(Dec(s.findtext("T4_AMT/empt_incamt")) for s in t4.findall("T4Slip"))
        assert amt_total == Dec(t4.findtext("T4Summary/T4_TAMT/tot_empt_incamt"))
        for s in t4.findall("T4Slip"):
            assert s.findtext("bn") == t4.findtext("T4Summary/bn")
            qc = s.findtext("empt_prov_cd") == "QC"
            assert (s.find("T4_AMT/cpp_cntrb_amt") is None) or not qc
            assert (s.find("T4_AMT/qpp_cntrb_amt") is None) or qc
            assert len(s.findtext("sin")) == 9


def test_rl1_matches_t4_and_reconciles_to_remittances(qc_year):
    _, runs = qc_year
    rl1 = {r["employee_id"]: r for r in rl1_slips(runs)}
    t4 = {s["employee_id"]: s for s in t4_slips(runs)}
    for eid, r in rl1.items():
        assert r["A_employment_income"] == t4[eid]["box14_employment_income"]
        assert r["B.A_qpp_contribution"] == t4[eid]["box17_qpp"]
        assert r["B.B_qpp_second_additional"] == t4[eid]["box17A_qpp2"]
        assert r["H_qpip_premium"] == t4[eid]["box55_ppip"]
    assert rl1["Q02"]["G_qpp_pensionable_salary"] == Dec("85000")     # B.B present -> YAMPE cap
    assert rl1["Q01"]["G_qpp_pensionable_salary"] == Dec("72000.00")
    tot = rl1_totals(runs, list(rl1.values()))
    assert tot["total_due_to_revenu_quebec"] == tot["remitted_in_year"]


def test_ontario_year_has_no_rl1(on_year):
    assert rl1_slips(on_year[1]) == []


def test_roe_for_laid_off_employee(qc_year):
    inputs, runs = qc_year
    e = next(x for x in inputs.employees if x.id == "Q06")
    roe = roe_for(e, runs, inputs.company)
    pp = roe["block15C_insurable_earnings_by_pay_period"]
    assert pp[0]["pay_period"] == 1 and pp[0]["period_end"] == "2026-10-31"      # final pay period first
    assert pp[0]["insurable_hours"] == Dec("45") and pp[0]["insurable_earnings"] == Dec("1192.50")
    assert pp[-1]["period_end"] == "2026-01-15" and pp[-1]["insurable_hours"] == Dec("30")
    assert len(pp) == 20                                                         # Jan 12 to Oct 23: 20 periods
    assert roe["block15A_total_insurable_hours"] == sum(p["insurable_hours"] for p in pp)
    assert roe["block15B_total_insurable_earnings"] == sum(p["insurable_earnings"] for p in pp[:13])
    assert roe["block11_last_day_paid"] == "2026-10-23" and roe["block16_reason_code"] == "A"
    assert "SAMPLE" in roe["label"] and roe["flags"]
