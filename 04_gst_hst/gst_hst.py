"""Deterministic core of the GST/HST return agent.

Everything here is plain code: loading the ledger export and the rules file,
applying the rules to each classified transaction, raising flags, computing the
GST34 lines and the filing deadline. The model never touches these numbers.
Amounts are integer cents throughout.
"""

from __future__ import annotations

import calendar
import csv
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from shared.tax import RATES

MODULE_DIR = Path(__file__).resolve().parent
ROOT = MODULE_DIR.parent
RULES_PATH = MODULE_DIR / "rules.yaml"
DATA_DIR = ROOT / "data" / "synthetic" / "gst_hst"

SUPPLY_TYPES = ["taxable", "zero_rated", "exempt", "out_of_scope"]
ITC_TREATMENTS = ["full", "restricted_50", "none", "not_applicable"]
PLACES = ["ON", "AB", "BC", "QC", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU",
          "OUTSIDE_CANADA", "NOT_APPLICABLE", "UNKNOWN"]
LINE_ORDER = ["101", "103", "104", "105", "106", "107", "108", "109", "110", "111", "112",
              "113A", "205", "405", "113B", "113C", "114", "115"]
BN_RE = re.compile(r"^\d{9}RT\d{4}$")


# --------------------------------------------------------------------------- money

def to_cents(s: str) -> int:
    """'1,234.56' or '-12.3' -> integer cents, exact (no float)."""
    s = (s or "0").replace(",", "").replace("$", "").strip() or "0"
    return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def mul_round(cents: int, fraction: float | Decimal) -> int:
    """cents * fraction, rounded half-up to the cent (half away from zero for negatives)."""
    v = Decimal(cents) * Decimal(str(fraction))
    return int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(c: int) -> str:
    sign = "-" if c < 0 else ""
    return f"{sign}${abs(c) / 100:,.2f}"


# --------------------------------------------------------------------------- data

@dataclass
class Txn:
    txn_id: str
    date: date
    description: str
    amount_cents: int      # signed bank movement: + money in, - money out
    net_cents: int         # unsigned, before GST/HST
    tax_cents: int         # unsigned GST/HST shown on the source document
    supplier_bn: str = ""

    @property
    def direction(self) -> str:
        return "in" if self.amount_cents > 0 else "out"

    @property
    def gross_cents(self) -> int:
        return abs(self.amount_cents)


@dataclass
class Classification:
    category: str
    supply_type: str
    place_of_supply: str
    itc_treatment: str
    confidence: float
    evidence: str = ""
    needs_human_reason: str = ""


@dataclass
class Treated:
    txn: Txn
    cls: Classification
    supply_type: str          # from the rules for the category (authoritative)
    itc_treatment: str
    place: str
    rate: float = 0.0
    lines: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    held_out: bool = False    # e.g. duplicate: excluded from the return until a human decides


def load_transactions(path: Path = DATA_DIR / "transactions.csv") -> list[Txn]:
    out = []
    with Path(path).open(newline="") as f:
        for r in csv.DictReader(f):
            out.append(Txn(r["txn_id"], date.fromisoformat(r["date"]), r["description"],
                           to_cents(r["amount"]), to_cents(r["net"]), to_cents(r["gst_hst"]),
                           r.get("supplier_bn", "").strip()))
    return out


def load_labels(path: Path = DATA_DIR / "labels.csv") -> dict[str, dict]:
    with Path(path).open(newline="") as f:
        return {r["txn_id"]: r for r in csv.DictReader(f)}


def gold_classifications(labels: dict[str, dict]) -> dict[str, Classification]:
    """The hand-labelled answers as Classifications (confidence 1.0), for evals and tests."""
    return {k: Classification(r["category"], r["supply_type"], r["place_of_supply"], r["itc_treatment"], 1.0, "gold")
            for k, r in labels.items()}


def load_business(path: Path = DATA_DIR / "business.json") -> dict:
    import json
    return json.loads(Path(path).read_text())


# --------------------------------------------------------------------------- rules

class Rules:
    """rules.yaml, loaded and compiled once, then applied to every transaction."""

    def __init__(self, path: Path = RULES_PATH):
        self.raw = yaml.safe_load(Path(path).read_text())
        self.categories: dict[str, dict] = self.raw["categories"]
        self.t = self.raw["thresholds"]
        self.keyword_res = {
            name: [re.compile(k, re.IGNORECASE) for k in c.get("keywords", [])]
            for name, c in self.categories.items()
        }
        pos = self.raw["place_of_supply"]
        self.home = pos["home_province"]
        self.province_res = {p: [re.compile(k) for k in ks] for p, ks in pos["province_tokens"].items()}
        self.outside_res = [re.compile(k) for k in pos["outside_canada_tokens"]]
        self._validate()

    def _validate(self) -> None:
        for name, c in self.categories.items():
            assert c["supply_type"] in SUPPLY_TYPES, name
            assert c["itc_treatment"] in ITC_TREATMENTS, name
            assert c["direction"] in ("in", "out", "any"), name
        for k in ("out", "in"):
            assert self.raw["fallback"][k] in self.categories

    def category(self, name: str) -> dict:
        return self.categories[name]

    def itc_fraction(self, treatment: str) -> float:
        return float(self.raw["itc_treatments"][treatment]["fraction"])

    def place_from_text(self, text: str) -> str:
        for p, res in self.province_res.items():
            if any(r.search(text) for r in res):
                return p
        if any(r.search(text) for r in self.outside_res):
            return "OUTSIDE_CANADA"
        return "UNKNOWN"

    def source_url(self, key: str) -> str | None:
        s = self.raw["sources"].get(key)
        return s["url"] if s else None


# --------------------------------------------------------------------------- engine

def treat(txns: list[Txn], classes: dict[str, Classification], rules: Rules) -> list[Treated]:
    """Apply the rules to each classified transaction: lines, ITCs and flags."""
    t = rules.t
    out: list[Treated] = []
    seen: dict[tuple, str] = {}
    for x in txns:
        c = classes[x.txn_id]
        cat = rules.category(c.category)
        tr = Treated(x, c, cat["supply_type"], cat["itc_treatment"], c.place_of_supply)
        flags = tr.flags

        # Classifier quality.
        if c.confidence < t["low_confidence"]:
            flags.append(f"low confidence ({c.confidence:.2f}): {c.needs_human_reason or 'check category'}")
        if c.supply_type != tr.supply_type or c.itc_treatment != tr.itc_treatment:
            flags.append(f"rule conflict: extractor said {c.supply_type}/{c.itc_treatment}, "
                         f"rules for {c.category} say {tr.supply_type}/{tr.itc_treatment} (rules used)")
        if cat["direction"] != "any" and cat["direction"] != x.direction:
            flags.append(f"rule conflict: {c.category} expects money {cat['direction']}, bank shows money {x.direction}")
        if cat.get("always_flag"):
            flags.append(cat["always_flag"])

        # Arithmetic on the source document.
        if x.net_cents + x.tax_cents != x.gross_cents:
            flags.append(f"totals mismatch: net {fmt(x.net_cents)} + tax {fmt(x.tax_cents)} != bank {fmt(x.gross_cents)}")

        key = (x.date, x.amount_cents, x.description.strip().lower())
        if key in seen:
            tr.held_out = True
            flags.append(f"duplicate of {seen[key]}: excluded from the return until confirmed")
        else:
            seen[key] = x.txn_id

        lines = tr.lines
        if not tr.held_out:
            _lines_for(tr, cat, rules)
        out.append(tr)
        # Flags that depend on the computed ITC.
        itc = lines.get("106", 0)
        if itc > 0:
            if not x.supplier_bn and x.gross_cents >= t["bn_flag_cents"]:
                level = ("required by SOR/91-45 s. 3(b); ITC at risk" if x.gross_cents >= t["bn_required_cents"]
                         else "advisory: legally required from $100")
                flags.append(f"ITC {fmt(itc)} claimed without supplier registration number ({level})")
            elif x.supplier_bn and not BN_RE.match(x.supplier_bn):
                flags.append(f"supplier registration number '{x.supplier_bn}' is not 9 digits + RT + 4 digits")
    return out


def _lines_for(tr: Treated, cat: dict, rules: Rules) -> None:
    x, t, lines = tr.txn, rules.t, tr.lines
    tol = t["rate_tolerance_cents"]
    name = tr.cls.category

    if name in ("sale_domestic", "sale_export", "sale_exempt_residential_rent", "credit_note"):
        place = tr.place
        if tr.supply_type == "taxable":
            if place not in RATES:
                tr.flags.append(f"place of supply '{place}' not a province; {rules.home} assumed")
                place = rules.home
                tr.place = place
            tr.rate = RATES[place][0]
        collectible = mul_round(x.net_cents, tr.rate)
        if abs(collectible - x.tax_cents) > tol:
            msg = (f"rate mismatch: {fmt(x.tax_cents)} charged, {fmt(collectible)} expected at "
                   f"{tr.rate:.0%} for {place}")
            if x.tax_cents > collectible and name != "credit_note":
                msg += "; the amount collected is reported (ETA s. 225(1)) unless the customer is refunded (s. 232)"
            tr.flags.append(msg)
        if name == "credit_note":
            lines["101"] = -x.net_cents
            lines["107"] = x.tax_cents
            if x.gross_cents >= t["large_refund_cents"]:
                tr.flags.append(f"large refund {fmt(x.gross_cents)} (threshold {fmt(t['large_refund_cents'])}); "
                                "check the credit note and original invoice")
        else:
            lines["101"] = x.net_cents
            # ETA s. 225(1): tax collectible, plus any amount collected as tax beyond it.
            tax = max(collectible, x.tax_cents) if tr.supply_type == "taxable" else x.tax_cents
            if tax:
                lines["103"] = tax
        return

    if tr.supply_type in ("exempt", "out_of_scope") and x.tax_cents and name != "foreign_digital_service":
        tr.flags.append(f"tax {fmt(x.tax_cents)} shown on a supply the rules treat as {tr.supply_type}")

    if name == "foreign_digital_service":
        commercial = cat.get("default_commercial_use_pct", 100)
        if commercial < t["exclusive_commercial_use_pct"]:
            lines["405"] = mul_round(x.net_cents, RATES[rules.home][0])
        if x.tax_cents:
            tr.flags.append(f"vendor charged {fmt(x.tax_cents)} GST/HST: not claimable as ITC (ETA s. 211.17); "
                            "ask vendor for a s. 232 refund")
        return

    if x.direction == "out":
        if x.tax_cents:
            ratio_ok = any(abs(mul_round(x.net_cents, r) - x.tax_cents) <= tol for r in t["valid_purchase_rates"])
            if not ratio_ok:
                tr.flags.append(f"rate mismatch: tax {fmt(x.tax_cents)} on {fmt(x.net_cents)} is not a GST/HST rate")
        itc = mul_round(x.tax_cents, rules.itc_fraction(tr.itc_treatment))
        if itc:
            lines["106"] = itc


def compute_gst34(treated: list[Treated], *, instalments_cents: int = 0, rebates_cents: int = 0,
                  real_property_cents: int = 0) -> dict[str, int]:
    """GST34 lines (regular method) from the per-transaction line contributions."""
    L = {k: 0 for k in LINE_ORDER}
    for tr in treated:
        for k, v in tr.lines.items():
            L[k] += v
    L["104"] = 0
    L["105"] = L["103"] + L["104"]
    L["108"] = L["106"] + L["107"]
    L["109"] = L["105"] - L["108"]
    L["110"] = instalments_cents
    L["111"] = rebates_cents
    L["112"] = L["110"] + L["111"]
    L["113A"] = L["109"] - L["112"]
    L["205"] = real_property_cents
    L["113B"] = L["205"] + L["405"]
    L["113C"] = L["113A"] + L["113B"]
    L["114"] = -L["113C"] if L["113C"] < 0 else 0
    L["115"] = L["113C"] if L["113C"] > 0 else 0
    return L


# --------------------------------------------------------------------------- deadline

def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def cra_holidays(y: int, province: str = "ON") -> set[date]:
    """Public holidays the CRA lists for due dates (outside Quebec)."""
    easter = _easter(y)
    may24 = date(y, 5, 24)
    victoria = may24 - timedelta(days=may24.weekday())  # Monday on or before May 24
    h = {
        date(y, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1), victoria,
        date(y, 7, 1), _nth_weekday(y, 9, 0, 1), date(y, 9, 30), _nth_weekday(y, 10, 0, 2),
        date(y, 11, 11), date(y, 12, 25), date(y, 12, 26),
    }
    if province == "QC":
        h.add(date(y, 6, 24))
    else:
        h.add(_nth_weekday(y, 8, 0, 1))  # Civic Holiday
    return h


def add_one_month(d: date) -> date:
    """One month after d; a period ending on a month's last day is due on the next month's last day."""
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    last = calendar.monthrange(y, m)[1]
    if d.day == calendar.monthrange(d.year, d.month)[1]:
        return date(y, m, last)
    return date(y, m, min(d.day, last))


def filing_deadline(period_end: date, province: str = "ON") -> tuple[date, date]:
    """(statutory due date, effective date after weekend/holiday roll-forward)."""
    due = add_one_month(period_end)
    eff = due
    while eff.weekday() >= 5 or eff in cra_holidays(eff.year, province):
        eff += timedelta(days=1)
    return due, eff
