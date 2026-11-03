"""Deterministic payroll deductions for Ontario employees, per CRA T4127 (2026).

Everything here is plain Decimal arithmetic. No model is involved. The names of the
variables follow the T4127 glossary (P, I, B, PI, IE, C, C2, EI, A, K1..K4, T1, T2,
T4, V1, V2, S) so the code can be read side by side with the publication.

Scope and simplifications (also listed in the module README):
- Ontario only, employees aged 18-70 contributing CPP for all 12 months (PM = 12).
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


RATES = load_rates()


# ---------------------------------------------------------------- inputs / outputs

@dataclass
class YTD:
    """Year-to-date amounts before this pay period."""
    pensionable: Decimal = D0   # PIYTD
    cpp: Decimal = D0           # D  (base + first additional)
    cpp2: Decimal = D0          # D2
    ei: Decimal = D0            # D1
    insurable: Decimal = D0


@dataclass
class PeriodInput:
    periods_per_year: int       # P
    regular: Decimal            # I: periodic gross for the period
    bonus: Decimal = D0         # B: non-periodic payment in this period
    federal_claim: Decimal | None = None   # TC from TD1; None = basic (BPAF)
    ontario_claim: Decimal | None = None   # TCP from TD1ON; None = basic personal amount
    dependants_y: int = 0       # count for the Ontario tax reduction (Y)
    ytd: YTD = field(default_factory=YTD)


@dataclass
class Deductions:
    cpp: Decimal
    cpp2: Decimal
    ei: Decimal
    federal_tax: Decimal
    ontario_tax: Decimal
    employer_cpp: Decimal
    employer_cpp2: Decimal
    employer_ei: Decimal
    cpp_max_reached: bool       # the cap bound this period (C limited by the annual max)
    cpp2_max_reached: bool
    ei_max_reached: bool
    detail: dict

    @property
    def income_tax(self) -> Decimal:
        return self.federal_tax + self.ontario_tax

    @property
    def employee_total(self) -> Decimal:
        return self.cpp + self.cpp2 + self.ei + self.income_tax

    @property
    def remittance(self) -> Decimal:
        """What this line adds to the PD7A: employee + employer CPP/CPP2/EI, plus tax."""
        return (self.cpp + self.employer_cpp + self.cpp2 + self.employer_cpp2
                + self.ei + self.employer_ei + self.income_tax)

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
        cpp=C, cpp2=C2, ei=EI, federal_tax=fed_tax, ontario_tax=on_tax,
        employer_cpp=C, employer_cpp2=C2,
        employer_ei=money(EI * r["ei"]["employer_multiplier"]),
        cpp_max_reached=cpp_hit, cpp2_max_reached=cpp2_hit, ei_max_reached=ei_hit,
        detail={"PI": PI, "IE": IE, "F5": F5, "F5A": F5A, "F5B": F5B,
                "without_bonus": without, "with_bonus": with_bonus},
    )
