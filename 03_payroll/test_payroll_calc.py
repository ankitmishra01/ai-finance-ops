"""Deduction maths, checked against values worked by hand from the T4127 formulas.

Hand calculations are written out in the docstrings. Cross-check any change against
the CRA Payroll Deductions Online Calculator (PDOC):
https://www.canada.ca/en/revenue-agency/services/e-services/e-services-businesses/payroll-deductions-online-calculator.html
"""

from decimal import Decimal as Dec

import pytest

from payroll_calc import (RATES, YTD, PeriodInput, annual_tax, basic_exemption_per_period, bpaf, compute,
                          cpp2_contribution, cpp_contribution, ei_premium, money, ontario_health_premium,
                          ontario_surtax)


def test_rates_file_is_2026_and_sourced():
    assert RATES["year"] == 2026
    assert RATES["cpp"]["max_contribution"] == Dec("4230.45")
    assert RATES["cpp2"]["max_contribution"] == Dec("416.00")
    assert RATES["ei"]["max_premium"] == Dec("1123.07")
    assert RATES["federal"]["brackets"][0]["r"] == Dec("0.14")
    for url in RATES["sources"].values():
        assert url.startswith("https://www.canada.ca/")


def test_cra_rounding_half_up():
    assert money(Dec("169.825")) == Dec("169.83")
    assert money(Dec("169.8249")) == Dec("169.82")


def test_basic_exemption_truncates_like_table_6_1():
    assert basic_exemption_per_period(26) == Dec("134.61")
    assert basic_exemption_per_period(52) == Dec("67.30")
    assert basic_exemption_per_period(24) == Dec("145.83")


def test_salaried_72000_semi_monthly_basic_claim():
    """$72,000/yr, semi-monthly (P=24), TD1 basic, no YTD.

    I = 3,000.00
    CPP  C  = 0.0595 x (3000 - 145.83) = 169.823 -> 169.82
    CPP2 C2 = (0 + 3000 - W) x 0.04 with W = YMPE 74,600 -> negative -> 0
    EI      = 0.0163 x 3000 = 48.90 ; employer 1.4 x 48.90 = 68.46
    F5  = 169.82 x 0.0100/0.0595 = 28.5412
    A   = 24 x (3000 - 28.5412) = 71,315.01
    Federal: R = 0.205, K = 3,804 ; TC = BPAF = 16,452 (A < 181,440)
      K1 = 0.14 x 16,452 = 2,303.28
      K2 = 0.14 x [24 x 169.82 x 0.0495/0.0595 = 3,390.69] + 0.14 x [min(24 x 48.90, 1,123.07) = 1,123.07] = 631.93
      K4 = min(0.14 x 72,000, 0.14 x 1,501) = 210.14
      T1 = 0.205 x 71,315.01 - 3,804 - 2,303.28 - 631.93 - 210.14 = 7,670.23 ; /24 = 319.59
    Ontario: V = 0.0915, KP = 2,210
      K1P = 0.0505 x 12,989 = 655.94 ; K2P = 0.0505 x 4,513.76 = 227.94
      T4 = 0.0915 x 71,315.01 - 2,210 - 655.94 - 227.94 = 3,431.43 ; V1 = 0 (T4 <= 5,818)
      S  = max(0, 2 x 300 - 3,431.43) = 0
      V2 = min(600, 450 + 0.25 x (71,315.01 - 48,000)) = 600
      T2 = 3,431.43 + 600 = 4,031.43 ; /24 = 167.98
    """
    d = compute(PeriodInput(24, Dec("3000.00")))
    assert (d.cpp, d.cpp2, d.ei) == (Dec("169.82"), Dec("0.00"), Dec("48.90"))
    assert d.employer_ei == Dec("68.46")
    assert d.employer_cpp == d.cpp
    assert d.federal_tax == Dec("319.59")
    assert d.ontario_tax == Dec("167.98")
    assert d.income_tax == Dec("487.57")


def test_td1_claim_above_basic():
    """$68,000/yr, P=24, TD1 federal claim 31,000, TD1ON claim 23,000.

    I = 2,833.33 ; C = 0.0595 x 2,687.50 = 159.906 -> 159.91 ; EI = 0.0163 x 2,833.33 = 46.18
    F5 = 159.91 x 0.0100/0.0595 = 26.8756 ; A = 24 x (2,833.33 - 26.8756) = 67,354.90
    CPP/EI credit base = 24 x 159.91 x 0.0495/0.0595 + 24 x 46.18 = 3,192.82 + 1,108.32 = 4,301.14
    Federal: T1 = 0.205 x 67,354.90 - 3,804 - 0.14 x 31,000 - 0.14 x 4,301.14 - 210.14
              = 13,807.76 - 3,804 - 4,340 - 602.16 - 210.14 = 4,851.46 ; /24 = 202.14
    Ontario: T4 = 0.0915 x 67,354.90 - 2,210 - 0.0505 x 23,000 - 0.0505 x 4,301.14
              = 6,162.97 - 2,210 - 1,161.50 - 217.21 = 2,574.27 ; V1 = 0 ; S = 0 ; V2 = 600
             T2 = 3,174.27 ; /24 = 132.26
    """
    d = compute(PeriodInput(24, Dec("2833.33"), federal_claim=Dec("31000"), ontario_claim=Dec("23000")))
    assert (d.cpp, d.ei) == (Dec("159.91"), Dec("46.18"))
    assert d.federal_tax == Dec("202.14")
    assert d.ontario_tax == Dec("132.26")
    basic = compute(PeriodInput(24, Dec("2833.33")))
    assert basic.income_tax > d.income_tax


def test_cpp_capped_at_annual_max():
    # 4,230.45 - 4,041.00 = 189.45 left; the formula would give 202.05.
    c, hit = cpp_contribution(Dec("3541.67"), 24, Dec("4041.00"))
    assert c == Dec("189.45") and hit
    c, hit = cpp_contribution(Dec("3541.67"), 24, Dec("4230.45"))
    assert c == Dec("0.00") and not hit


def test_cpp_never_negative_on_tiny_pay():
    c, _ = cpp_contribution(Dec("100"), 24, Dec("0"))
    assert c == Dec("0.00")


def test_cpp2_starts_above_ympe_and_caps():
    # PIYTD 72,000 + PI 4,000 = 76,000 ; W = 74,600 ; C2 = 1,400 x 0.04 = 56.00
    c2, hit = cpp2_contribution(Dec("4000"), Dec("72000"), Dec("0"))
    assert c2 == Dec("56.00") and not hit
    # Already above YMPE: W = PIYTD, so the whole PI attracts 4%.
    c2, _ = cpp2_contribution(Dec("5458.33"), Dec("79166.60"), Dec("182.66"))
    assert c2 == Dec("218.33")
    # Near the 416 cap: 416 - 400 = 16 left.
    c2, hit = cpp2_contribution(Dec("5000"), Dec("80000"), Dec("400"))
    assert c2 == Dec("16.00") and hit


def test_ei_capped():
    ei, hit = ei_premium(Dec("2666.67"), Dec("1086.60"))
    assert ei == Dec("36.47") and hit           # 1,123.07 - 1,086.60
    ei, hit = ei_premium(Dec("1000"), Dec("1123.07"))
    assert ei == Dec("0.00") and not hit


def test_bpaf_phase_out():
    assert bpaf(Dec("181440")) == Dec("16452")
    assert bpaf(Dec("258482")) == Dec("14829")
    # Midpoint 219,961: 16,452 - 38,521 x 1,623/77,042 = 16,452 - 811.50 = 15,640.50
    assert bpaf(Dec("219961")) == Dec("15640.50")


@pytest.mark.parametrize("A, expected", [
    ("20000", "0"),         # no premium at or below 20,000
    ("25000", "300"),       # min(300, 0.06 x 5,000)
    ("30000", "300"),       # min(300, 0.06 x 10,000 = 600)
    ("38000", "420"),       # min(450, 300 + 0.06 x 2,000)
    ("40000", "450"),       # min(450, 300 + 0.06 x 4,000 = 540)
    ("49000", "600"),       # min(600, 450 + 0.25 x 1,000 = 700)
    ("72500", "725"),       # min(750, 600 + 0.25 x 500)
    ("150000", "750"),      # cap 750
    ("210000", "900"),      # min(900, 750 + 0.25 x 10,000)
])
def test_ontario_health_premium(A, expected):
    assert ontario_health_premium(Dec(A)) == Dec(expected)


def test_ontario_surtax():
    assert ontario_surtax(Dec("5818")) == 0
    assert ontario_surtax(Dec("7000")) == Dec("236.40")                 # 0.20 x 1,182
    # 0.20 x (10,000 - 5,818) + 0.36 x (10,000 - 7,446) = 836.40 + 919.44
    assert ontario_surtax(Dec("10000")) == Dec("1755.84")


def test_ontario_tax_reduction_clears_low_income_tax():
    # A = 20,000 (no CPP/EI credit): T4 = 0.0505 x (20,000 - 12,989) = 354.0555 ;
    # S = min(354.0555, 600 - 354.0555 = 245.9445) ; V2 = 0 ; T2 = 2 x 354.0555 - 600 = 108.111
    t = annual_tax(Dec("20000"), P=24, cpp_base_annual=Dec("0"), ei_annual=Dec("0"), gross_annual=Dec("20000"),
                   federal_claim=None, ontario_claim=None)
    assert money(t["T4"]) == Dec("354.06")
    assert money(t["S"]) == Dec("245.94")
    assert money(t["T2"]) == Dec("108.11")


def test_bonus_taxed_by_difference_method():
    base = compute(PeriodInput(24, Dec("3958.33")))
    with_bonus = compute(PeriodInput(24, Dec("3958.33"), bonus=Dec("1500")))
    extra = with_bonus.income_tax - base.income_tax
    # 1,500 of extra income sits in the 20.5% federal + 9.15% Ontario band: between 25% and 35% of it.
    assert Dec("375") < extra < Dec("525")
    # CPP and EI apply to the bonus as well.
    assert with_bonus.cpp > base.cpp and with_bonus.ei > base.ei


def test_remittance_identity():
    d = compute(PeriodInput(24, Dec("3000.00"), ytd=YTD()))
    assert d.remittance == 2 * d.cpp + 2 * d.cpp2 + d.ei + d.employer_ei + d.income_tax
