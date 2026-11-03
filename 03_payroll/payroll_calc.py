"""Deterministic payroll deductions for Ontario and Quebec employees (2026).

Ontario: CRA T4127. Quebec: QPP/QPP2, QPIP, EI at the Quebec rate and federal tax with
the 16.5% abatement per T4127, and Quebec income tax per Revenu Québec TP-1015.F-V.

Everything here is plain Decimal arithmetic. No model is involved. The names of the
variables follow the T4127 glossary (P, I, B, PI, IE, C, C2, EI, A, K1..K4, T1, T2,
T4, V1, V2, S) so the code can be read side by side with the publication.

Scope and simplifications (also listed in the module README):
- Ontario or Quebec (PeriodInput.province), employees aged 18-70 contributing CPP for all 12 months (PM = 12).
- No RPP, union dues, prescribed-zone or childcare deductions (F, F1, F2, U1, HD = 0).
- No labour-sponsored funds credit (LCF, LCP = 0) and no K3/K3P other credits.
- Bonuses use the T4127 "Option 1" difference method: annual tax with the bonus minus
  annual tax without it. K2/K2P in the with-bonus step adds the CPP base and EI on the
  bonus, as in the T4127 bonus example.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
D0 = Decimal("0")
CENT = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    """CRA rounding: increase the second decimal by one if the third digit is 5 or more."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _dec(obj):
    """Turn every string that looks like a number in the YAML into a Decimal."""
    if isinstance(obj, dict):
        return {k: _dec(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dec(v) for v in obj]
    if isinstance(obj, str):
        try:
            return Decimal(obj)
        except Exception:
            return obj
    return obj


def load_rates(path: Path = HERE / "rates_2026.yaml") -> dict:
    raw = yaml.safe_load(Path(path).read_text())
    rates = _dec(raw)
    rates["sources"] = raw["sources"]
    rates["edition"] = raw["edition"]
    return rates


def load_qc_rates(path: Path = HERE / "rates_2026_qc.yaml") -> dict:
    raw = yaml.safe_load(Path(path).read_text())
    rates = _dec(raw)
    rates["sources"] = raw["sources"]
    rates["edition"] = raw["edition"]
    return rates


RATES = load_rates()
QC = load_qc_rates()


# ---------------------------------------------------------------- inputs / outputs

@dataclass
class YTD:
    """Year-to-date amounts before this pay period."""
    pensionable: Decimal = D0   # PIYTD
    cpp: Decimal = D0           # D  (base + first additional)
    cpp2: Decimal = D0          # D2
    ei: Decimal = D0            # D1
    insurable: Decimal = D0
    # Quebec only. For a Quebec employee `cpp`/`cpp2` hold QPP/QPP2 (T4127 uses C for both).
    qpip: Decimal = D0          # A6
    employer_qpip: Decimal = D0  # A7
    lump_sums: Decimal = D0     # B1: bonuses and similar paid earlier in the year
    csb: Decimal = D0           # CSB1: QPP additional-contribution deduction on those


@dataclass
class PeriodInput:
    periods_per_year: int       # P
    regular: Decimal            # I: periodic gross for the period
    bonus: Decimal = D0         # B: non-periodic payment in this period
    federal_claim: Decimal | None = None   # TC from TD1; None = basic (BPAF)
    ontario_claim: Decimal | None = None   # TCP from TD1ON; None = basic personal amount
    dependants_y: int = 0       # count for the Ontario tax reduction (Y)
    ytd: YTD = field(default_factory=YTD)
    province: str = "ON"        # "ON" or "QC" (province of employment, T4 box 10)
    quebec_claim: Decimal | None = None    # E from TP-1015.3-V; None = basic personal amount


@dataclass
class Deductions:
    cpp: Decimal                # CPP, or QPP for a Quebec employee
    cpp2: Decimal               # CPP2, or QPP2
    ei: Decimal
    federal_tax: Decimal
    provincial_tax: Decimal     # Ontario tax, or Quebec tax
    employer_cpp: Decimal
    employer_cpp2: Decimal
    employer_ei: Decimal
    cpp_max_reached: bool       # the cap bound this period (C limited by the annual max)
    cpp2_max_reached: bool
    ei_max_reached: bool
    detail: dict
    province: str = "ON"
    qpip: Decimal = D0
    employer_qpip: Decimal = D0
    employer_hsf: Decimal = D0  # Quebec health services fund, employer only
    qpip_max_reached: bool = False

    @property
    def quebec(self) -> bool:
        return self.province == "QC"

    @property
    def ontario_tax(self) -> Decimal:
        return D0 if self.quebec else self.provincial_tax

    @property
    def income_tax(self) -> Decimal:
        return self.federal_tax + self.provincial_tax

    @property
    def employee_total(self) -> Decimal:
        return self.cpp + self.cpp2 + self.ei + self.qpip + self.income_tax

    @property
    def cra_remittance(self) -> Decimal:
        """What this line adds to the PD7A. Quebec: federal tax and EI only."""
        if self.quebec:
            return self.ei + self.employer_ei + self.federal_tax
        return (self.cpp + self.employer_cpp + self.cpp2 + self.employer_cpp2
                + self.ei + self.employer_ei + self.income_tax)

    @property
    def rq_remittance(self) -> Decimal:
        """What this line adds to the Revenu Québec remittance: QPP, QPP2 and QPIP
        (employee and employer), Quebec tax and the health services fund."""
        if not self.quebec:
            return D0
        return (self.cpp + self.employer_cpp + self.cpp2 + self.employer_cpp2 + self.qpip
                + self.employer_qpip + self.provincial_tax + self.employer_hsf)

    @property
    def remittance(self) -> Decimal:
        """Everything this line sends to a tax authority: CRA plus Revenu Québec."""
        return self.cra_remittance + self.rq_remittance

    @property
    def employer_cost(self) -> Decimal:
        return self.employer_cpp + self.employer_cpp2 + self.employer_ei + self.employer_qpip + self.employer_hsf

    def as_dict(self) -> dict:
        d = asdict(self)
        d["income_tax"] = self.income_tax
        d["employee_total"] = self.employee_total
        d["remittance"] = self.remittance
        return d


# ---------------------------------------------------------------- CPP / CPP2 / EI

def basic_exemption_per_period(P: int, r=RATES) -> Decimal:
    # T4127 table 6.1 truncates: 3500/26 = 134.615 -> 134.61; 3500/52 -> 67.30
    return (r["cpp"]["basic_exemption"] / P).quantize(CENT, rounding=ROUND_DOWN)


def cpp_contribution(PI: Decimal, P: int, ytd_cpp: Decimal, PM: int = 12, r=RATES) -> tuple[Decimal, bool]:
    """C = lesser of (i) max x PM/12 - D, (ii) 0.0595 x [PI - 3500/P]; not negative."""
    cap = r["cpp"]["max_contribution"] * PM / 12 - ytd_cpp
    calc = r["cpp"]["rate"] * (PI - basic_exemption_per_period(P, r))
    c = max(D0, min(cap, calc))
    return money(c), calc > cap and cap > 0


def cpp2_contribution(PI: Decimal, piytd: Decimal, ytd_cpp2: Decimal, PM: int = 12, r=RATES) -> tuple[Decimal, bool]:
    """C2 = lesser of (i) 416 x PM/12 - D2, (ii) (PIYTD + PI - W) x 0.04;
    W = greater of PIYTD and YMPE x PM/12. Earnings above YAMPE are capped by (i)."""
    cap = r["cpp2"]["max_contribution"] * PM / 12 - ytd_cpp2
    W = max(piytd, r["cpp"]["ympe"] * PM / 12)
    calc = (piytd + PI - W) * r["cpp2"]["rate"]
    c2 = max(D0, min(cap, calc))
    return money(c2), calc > cap and cap > 0


def ei_premium(IE: Decimal, ytd_ei: Decimal, r=RATES) -> tuple[Decimal, bool]:
    """EI = lesser of (i) 1,123.07 - D1, (ii) 0.0163 x IE."""
    cap = r["ei"]["max_premium"] - ytd_ei
    calc = r["ei"]["rate"] * IE
    ei = max(D0, min(cap, calc))
    return money(ei), calc > cap and cap > 0


# ---------------------------------------------------------------- income tax

def bpaf(NI: Decimal, r=RATES) -> Decimal:
    b = r["federal"]["bpaf"]
    if NI <= b["phase_out_start"]:
        return b["max"]
    if NI >= b["phase_out_end"]:
        return b["min"]
    span = b["phase_out_end"] - b["phase_out_start"]
    return money(b["max"] - (NI - b["phase_out_start"]) * ((b["max"] - b["min"]) / span))


def _bracket(A: Decimal, rows: list[dict], rate_key: str, const_key: str) -> tuple[Decimal, Decimal]:
    chosen = rows[0]
    for row in rows:
        if A >= row["a"]:
            chosen = row
    return chosen[rate_key], chosen[const_key]


def ontario_surtax(T4: Decimal, r=RATES) -> Decimal:
    """V1: 20% of basic Ontario tax over $5,818 plus 36% over $7,446."""
    return sum((s["rate"] * (T4 - s["threshold"]) for s in r["ontario"]["surtax"] if T4 > s["threshold"]), D0)


def ontario_health_premium(A: Decimal, r=RATES) -> Decimal:
    """V2, by annual taxable income. Not reduced by the Ontario tax reduction."""
    for row in r["ontario"]["health_premium"]:
        upper = row["upper"]
        if A > row["lower"] and (upper is None or A <= upper):
            return min(row["cap"], row["base"] + row["rate"] * (A - row["lower"]))
    return D0


def annual_tax(A: Decimal, *, P: int, cpp_base_annual: Decimal, ei_annual: Decimal, gross_annual: Decimal,
               federal_claim: Decimal | None, ontario_claim: Decimal | None, Y: int = 0,
               r=RATES) -> dict:
    """Annual federal (T1) and Ontario (T2) tax on annual taxable income A.

    cpp_base_annual is P x C x (0.0495/0.0595) before capping; ei_annual is P x EI.
    """
    fed, on = r["federal"], r["ontario"]
    A = max(D0, A)

    # Federal: T3 = R x A - K - K1 - K2 - K3 - K4 ; T1 = T3 - P x LCF (LCF = 0 here)
    R, K = _bracket(A, fed["brackets"], "r", "k")
    TC = federal_claim if federal_claim is not None else bpaf(A)
    K1 = fed["lowest_rate"] * TC
    cpp_credit_base = min(cpp_base_annual, r["cpp"]["max_base_contribution"])
    ei_credit_base = min(ei_annual, r["ei"]["max_premium"])
    K2 = fed["lowest_rate"] * cpp_credit_base + fed["lowest_rate"] * ei_credit_base
    K4 = min(fed["lowest_rate"] * gross_annual, fed["lowest_rate"] * fed["canada_employment_amount"])
    T3 = max(D0, R * A - K - K1 - K2 - K4)
    T1 = T3

    # Ontario: T4 = V x A - KP - K1P - K2P ; T2 = T4 + V1 + V2 - S
    V, KP = _bracket(A, on["brackets"], "v", "kp")
    TCP = ontario_claim if ontario_claim is not None else on["basic_personal_amount"]
    K1P = on["lowest_rate"] * TCP
    K2P = on["lowest_rate"] * cpp_credit_base + on["lowest_rate"] * ei_credit_base
    T4 = max(D0, V * A - KP - K1P - K2P)
    V1 = ontario_surtax(T4, r)
    tr = on["tax_reduction"]
    S = max(D0, min(T4 + V1, 2 * (tr["basic"] + tr["per_dependant"] * Y) - (T4 + V1)))
    V2 = ontario_health_premium(A, r)
    T2 = max(D0, T4 + V1 + V2 - S)

    return {"A": A, "R": R, "K": K, "TC": TC, "K1": K1, "K2": K2, "K4": K4, "T1": T1,
            "V": V, "KP": KP, "TCP": TCP, "K1P": K1P, "K2P": K2P, "T4": T4, "V1": V1,
            "S": S, "V2": V2, "T2": T2}


# ---------------------------------------------------------------- one pay period

def compute(inp: PeriodInput, r=RATES) -> Deductions:
    if inp.province == "QC":
        return compute_qc(inp, r=r)
    if inp.province != "ON":
        raise ValueError(f"province {inp.province!r} is not supported (ON or QC)")
    P = inp.periods_per_year
    I, B = D(inp.regular), D(inp.bonus)
    PI = I + B   # pensionable income for the period
    IE = I + B   # insurable earnings for the period
    y = inp.ytd

    C, cpp_hit = cpp_contribution(PI, P, y.cpp, r=r)
    C2, cpp2_hit = cpp2_contribution(PI, y.pensionable, y.cpp2, r=r)
    EI, ei_hit = ei_premium(IE, y.ei, r=r)

    # The same contributions computed on regular pay only, for the without-bonus step.
    C_reg, _ = cpp_contribution(I, P, y.cpp, r=r)
    C2_reg, _ = cpp2_contribution(I, y.pensionable, y.cpp2, r=r)
    EI_reg, _ = ei_premium(I, y.ei, r=r)

    # F5 = C x (0.0100/0.0595) + C2 ; F5A = F5 x (PI - B)/PI ; F5B = F5 x B/PI
    ratio_add = r["cpp"]["first_additional_rate"] / r["cpp"]["rate"]
    ratio_base = r["cpp"]["base_rate"] / r["cpp"]["rate"]
    F5 = C * ratio_add + C2
    F5A = F5 * (PI - B) / PI if PI else D0
    F5B = F5 * B / PI if PI else D0

    A_reg = max(D0, P * (I - F5A))
    common = dict(P=P, gross_annual=P * I, federal_claim=inp.federal_claim,
                  ontario_claim=inp.ontario_claim, Y=inp.dependants_y, r=r)
    without = annual_tax(A_reg, cpp_base_annual=P * C_reg * ratio_base, ei_annual=P * EI_reg, **common)

    fed_reg = without["T1"] / P
    on_reg = without["T2"] / P
    fed_bonus = on_bonus = D0
    with_bonus = None
    if B > 0:
        A_bonus = A_reg + max(D0, B - F5B)
        with_bonus = annual_tax(
            A_bonus,
            cpp_base_annual=P * C_reg * ratio_base + (C - C_reg) * ratio_base,
            ei_annual=P * EI_reg + (EI - EI_reg),
            **common,
        )
        fed_bonus = max(D0, with_bonus["T1"] - without["T1"])
        on_bonus = max(D0, with_bonus["T2"] - without["T2"])

    fed_tax = money(fed_reg + fed_bonus)
    on_tax = money(on_reg + on_bonus)

    return Deductions(
        cpp=C, cpp2=C2, ei=EI, federal_tax=fed_tax, provincial_tax=on_tax,
        employer_cpp=C, employer_cpp2=C2,
        employer_ei=money(EI * r["ei"]["employer_multiplier"]),
        cpp_max_reached=cpp_hit, cpp2_max_reached=cpp2_hit, ei_max_reached=ei_hit,
        detail={"PI": PI, "IE": IE, "F5": F5, "F5A": F5A, "F5B": F5B,
                "without_bonus": without, "with_bonus": with_bonus},
    )


# ================================================================ Quebec
#
# Federal side (T4127 chapter 4, "Only for employees in Quebec"):
#   F5Q = C x (0.0100/0.0630) + C2
#   K2Q = 0.14 x min(P x C x 0.0530/0.0630, 3,768.30) + 0.14 x min(P x EI, 895.70)
#         + 0.14 x min(P x IE x 0.0043, 442.90)
#   T3  = R x A - K - K1 - K2Q - K4 ;  T1 = T3 - 0.165 x T3
# Quebec side (TP-1015.F-V 2026-01, section 2.1.1 regular payments, 2.1.2 method 2 for
# lump sums): I = P x (G - F - H - CSA) - J - J1 ; Y = T x I - K - K1 - 0.14 x E
#   - (0.15 x P x Q) - (0.15 x P x Q1) ; A = Y / P + L. QPP and QPIP per sections 3 and 4.

def money_min_cent(x: Decimal) -> Decimal:
    """TP-1015.F-V rounding for QPP and QPIP: half-cent rounds up, and a result above 0
    but below one cent is withheld as $0.01."""
    m = money(x)
    return CENT if (x > 0 and m == 0) else m


def qpp_contribution(S3: Decimal, P: int, A5: Decimal, PM: int = 12, q=QC) -> tuple[Decimal, bool]:
    """C = lesser of 4,479.30 x PM/12 - A5 and 0.0630 x [S3 - 3,500/P] (TP-1015.F-V 3.1)."""
    cap = q["qpp"]["max_contribution"] * PM / 12 - A5
    exemption = (q["qpp"]["basic_exemption"] / P).quantize(CENT, rounding=ROUND_DOWN)
    calc = q["qpp"]["rate"] * (S3 - exemption)
    c = max(D0, min(cap, calc))
    return money_min_cent(c), calc > cap and cap > 0


def qpp2_contribution(S3: Decimal, S5: Decimal, A8: Decimal, PM: int = 12, q=QC) -> tuple[Decimal, bool]:
    """C2 = lesser of 416 x PM/12 - A8 and 0.04 x (S5 + S3 - W), W = max(S5, 74,600 x PM/12)."""
    cap = q["qpp2"]["max_contribution"] * PM / 12 - A8
    W = max(S5, q["qpp"]["ympe"] * PM / 12)
    calc = q["qpp2"]["rate"] * (S5 + S3 - W)
    c2 = max(D0, min(cap, calc))
    return money_min_cent(c2), calc > cap and cap > 0


def qpip_premium(S4: Decimal, A6: Decimal, q=QC) -> tuple[Decimal, bool]:
    """Ap = lesser of 442.90 - A6 and 0.00430 x S4 (TP-1015.F-V 4.1)."""
    cap = q["qpip"]["max_premium"] - A6
    calc = q["qpip"]["rate"] * S4
    ap = max(D0, min(cap, calc))
    return money_min_cent(ap), calc > cap and cap > 0


def qpip_employer_premium(S4: Decimal, A7: Decimal, q=QC) -> Decimal:
    """Ap1 = lesser of 620.06 - A7 and 0.00602 x S4 (TP-1015.F-V 4.2)."""
    return money(max(D0, min(q["qpip"]["max_employer_premium"] - A7, q["qpip"]["employer_rate"] * S4)))


def ei_premium_qc(IE: Decimal, ytd_ei: Decimal, q=QC) -> tuple[Decimal, bool]:
    """EI at the Quebec rate: lesser of 895.70 - D1 and 0.0130 x IE."""
    cap = q["ei"]["max_premium"] - ytd_ei
    calc = q["ei"]["rate"] * IE
    ei = max(D0, min(cap, calc))
    return money(ei), calc > cap and cap > 0


def federal_tax_qc(A: Decimal, *, qpp_base_annual: Decimal, ei_annual: Decimal, qpip_base_annual: Decimal,
                   gross_annual: Decimal, federal_claim: Decimal | None, r=RATES, q=QC) -> dict:
    """Annual federal tax for a Quebec employee (T4127 steps 2 and 3).

    qpp_base_annual is P x C x (0.0530/0.0630); ei_annual is P x EI; qpip_base_annual is
    P x IE x 0.0043 (the credit is on the premium rate times insurable earnings)."""
    fed = r["federal"]
    A = max(D0, A)
    R, K = _bracket(A, fed["brackets"], "r", "k")
    TC = federal_claim if federal_claim is not None else bpaf(A)
    K1 = fed["lowest_rate"] * TC
    K2Q = (fed["lowest_rate"] * min(qpp_base_annual, q["qpp"]["max_base_contribution"])
           + fed["lowest_rate"] * min(ei_annual, q["ei"]["max_premium"])
           + fed["lowest_rate"] * min(qpip_base_annual, q["qpip"]["max_premium"]))
    K4 = min(fed["lowest_rate"] * gross_annual, fed["lowest_rate"] * fed["canada_employment_amount"])
    T3 = max(D0, R * A - K - K1 - K2Q - K4)
    abatement = q["federal"]["abatement"] * T3
    T1 = max(D0, T3 - abatement)
    return {"A": A, "R": R, "K": K, "TC": TC, "K1": K1, "K2Q": K2Q, "K4": K4, "T3": T3,
            "abatement": abatement, "T1": T1}


def _qc_bracket(I: Decimal, q=QC) -> tuple[Decimal, Decimal]:
    """TP-1015.F-V brackets read 'more than $54,345', so a threshold value stays in the lower band."""
    chosen = q["quebec"]["brackets"][0]
    for row in q["quebec"]["brackets"]:
        if I > row["a"]:
            chosen = row
    return chosen["t"], chosen["k"]


def deduction_for_workers(D_: Decimal, P: int, q=QC) -> Decimal:
    """H = 0.06 x D, maximum 1,450 / P. Rounded to the cent, as in TP-1015.F-V appendix 1."""
    qq = q["quebec"]
    return money(min(qq["deduction_for_workers_rate"] * D_, qq["deduction_for_workers_max"] / P))


def quebec_tax_regular(*, P: int, G: Decimal, C: Decimal, C2: Decimal, S3: Decimal, B2: Decimal = D0,
                       F: Decimal = D0, J: Decimal = D0, J1: Decimal = D0, E: Decimal | None = None,
                       K1: Decimal = D0, Q: Decimal = D0, Q1: Decimal = D0, L: Decimal = D0, q=QC) -> dict:
    """Quebec income tax on regular pay for one period (TP-1015.F-V 2.1.1).

    G: gross regular pay for the period; C, C2: QPP and QPP2 for the period (on S3, which
    includes any lump sum B2). Returns the step values and A, the tax for the period."""
    qq = q["quebec"]
    H = deduction_for_workers(G, P, q)
    CS = C * (q["qpp"]["first_additional_rate"] / q["qpp"]["rate"]) + C2
    CSA = money(CS * (S3 - B2) / S3) if S3 else D0
    I = max(D0, P * (G - F - H - CSA) - J - J1)
    T, K = _qc_bracket(I, q)
    E_ = (E if E is not None else qq["basic_personal_amount"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    Y = max(D0, T * I - K - K1 - qq["credit_rate"] * E_ - Decimal("0.15") * P * Q - Decimal("0.15") * P * Q1)
    A = max(D0, Y / P + L)
    return {"H": H, "CS": CS, "CSA": CSA, "I": I, "T": T, "K": K, "E": E_, "Y": Y, "A": money(A)}


def quebec_tax_lump_sum(*, P: int, G: Decimal, C: Decimal, C2: Decimal, S3: Decimal, B2: Decimal,
                        B1: Decimal = D0, CSB1: Decimal = D0, F: Decimal = D0, F_lump: Decimal = D0,
                        J: Decimal = D0, J1: Decimal = D0, q=QC) -> dict:
    """Quebec income tax on a bonus or similar lump sum, method 2 (TP-1015.F-V 2.1.2).

    I = P x (G - F - H - CSA) + B1 + B2 - CSB1 - CSB - J - J1 ; A = T x (B2 - CSB).
    If annual pay plus the lump sum is at most $18,952, 7% is withheld instead.
    F_lump is the part of F (e.g. RPP) taken on the lump sum; the guide reduces B2 by it
    in I and A, but CSA and CSB still use the full lump sum (appendix 2)."""
    qq = q["quebec"]
    if B2 <= 0:
        return {"A": D0, "CSB": D0, "method": "none"}
    H = deduction_for_workers(G, P, q)
    CS = C * (q["qpp"]["first_additional_rate"] / q["qpp"]["rate"]) + C2
    CSA = money(CS * (S3 - B2) / S3) if S3 else D0
    CSB = money(CS * B2 / S3) if S3 else D0
    if P * G + B1 + B2 <= qq["lump_sum_low_income_threshold"]:
        return {"A": money(qq["lump_sum_low_income_rate"] * B2), "CSB": CSB, "method": "flat 7%"}
    B2_net = B2 - F_lump
    I = max(D0, P * (G - F - H - CSA) + B1 + B2_net - CSB1 - CSB - J - J1)
    T, _ = _qc_bracket(I, q)
    return {"H": H, "CSA": CSA, "I": I, "T": T, "CSB": CSB, "A": money(T * (B2_net - CSB)), "method": "method 2"}


def compute_qc(inp: PeriodInput, r=RATES, q=QC) -> Deductions:
    """One pay period for an employee whose province of employment is Quebec."""
    P = inp.periods_per_year
    I, B = D(inp.regular), D(inp.bonus)
    S3 = I + B      # QPP pensionable salary, including the lump sum
    IE = I + B      # EI and QPIP insurable earnings
    y = inp.ytd

    C, qpp_hit = qpp_contribution(S3, P, y.cpp, q=q)
    C2, qpp2_hit = qpp2_contribution(S3, y.pensionable, y.cpp2, q=q)
    EI, ei_hit = ei_premium_qc(IE, y.ei, q=q)
    QP, qpip_hit = qpip_premium(IE, y.qpip, q=q)
    QP_er = qpip_employer_premium(IE, y.employer_qpip, q=q)

    # Regular-pay-only contributions for the federal without-bonus step (as in compute()).
    C_reg, _ = qpp_contribution(I, P, y.cpp, q=q)
    EI_reg, _ = ei_premium_qc(I, y.ei, q=q)

    ratio_add = q["qpp"]["first_additional_rate"] / q["qpp"]["rate"]
    ratio_base = q["qpp"]["base_rate"] / q["qpp"]["rate"]
    qpip_rate = q["qpip"]["rate"]
    F5Q = C * ratio_add + C2
    F5A = F5Q * (S3 - B) / S3 if S3 else D0
    F5B = F5Q * B / S3 if S3 else D0

    A_reg = max(D0, P * (I - F5A))
    common = dict(gross_annual=P * I, federal_claim=inp.federal_claim, r=r, q=q)
    without = federal_tax_qc(A_reg, qpp_base_annual=P * C_reg * ratio_base, ei_annual=P * EI_reg,
                             qpip_base_annual=P * I * qpip_rate, **common)
    fed_bonus = D0
    with_bonus = None
    if B > 0:
        with_bonus = federal_tax_qc(
            A_reg + max(D0, B - F5B),
            qpp_base_annual=P * C_reg * ratio_base + (C - C_reg) * ratio_base,
            ei_annual=P * EI_reg + (EI - EI_reg),
            qpip_base_annual=P * I * qpip_rate + B * qpip_rate,
            **common,
        )
        fed_bonus = max(D0, with_bonus["T1"] - without["T1"])
    fed_tax = money(without["T1"] / P + fed_bonus)

    qc_reg = quebec_tax_regular(P=P, G=I, C=C, C2=C2, S3=S3, B2=B, E=inp.quebec_claim, q=q)
    qc_lump = quebec_tax_lump_sum(P=P, G=I, C=C, C2=C2, S3=S3, B2=B, B1=y.lump_sums, CSB1=y.csb, q=q)
    qc_tax = qc_reg["A"] + qc_lump["A"]

    hsf = money(q["health_services_fund"]["rate_total_payroll_1m_or_less"] * (I + B))

    return Deductions(
        cpp=C, cpp2=C2, ei=EI, federal_tax=fed_tax, provincial_tax=qc_tax,
        employer_cpp=C, employer_cpp2=C2,
        employer_ei=money(EI * q["ei"]["employer_multiplier"]),
        cpp_max_reached=qpp_hit, cpp2_max_reached=qpp2_hit, ei_max_reached=ei_hit,
        province="QC", qpip=QP, employer_qpip=QP_er, employer_hsf=hsf, qpip_max_reached=qpip_hit,
        detail={"S3": S3, "IE": IE, "F5Q": F5Q, "F5A": F5A, "F5B": F5B,
                "federal_without_bonus": without, "federal_with_bonus": with_bonus,
                "quebec_regular": qc_reg, "quebec_lump_sum": qc_lump},
    )
