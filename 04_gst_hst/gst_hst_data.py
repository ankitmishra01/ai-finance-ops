"""Seeded generator for one quarter of a synthetic Ontario registrant's ledger export.

Writes to data/synthetic/gst_hst/:
  business.json        the registrant profile and reporting period
  transactions.csv     ~120 rows, what the agent sees (no labels)
  labels.csv           gold labels for every row, plus the 30-row eval subset
  expected_gst34.json  GST34 line totals worked out here from the gold labels,
                       independently of the agent's engine
  expected_qst.json    the QST return lines (Revenu Quebec FPZ-500, lines 203-209),
                       worked out the same way

Run: uv run python 04_gst_hst/gst_hst_data.py
All names are synthetic (Faker en_CA, seed 42). No real people or companies.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from faker import Faker

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "synthetic" / "gst_hst"

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 10, 31)
MONTHS = [(2026, 8), (2026, 9), (2026, 10)]
RATE = {"ON": Decimal("0.13"), "AB": Decimal("0.05"), "BC": Decimal("0.05"), "QC": Decimal("0.05")}
QST_RATE = Decimal("0.09975")   # shared/tax.py cites the Revenu Quebec source
# Quebec rows use their own random stream, drawn after everything else, so adding them did not
# change any earlier row, the 30-row eval set or the business number.
QST_SEED = 4242
CITIES = {
    "ON": ["Toronto", "Ottawa", "Mississauga", "Hamilton", "Kingston", "Waterloo"],
    "AB": ["Calgary", "Edmonton", "Red Deer"],
    "BC": ["Vancouver", "Victoria", "Kelowna"],
}


def r2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Gen:
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        Faker.seed(seed)
        self.fk = Faker("en_CA")
        self.rows: list[dict] = []
        self.inv = 4100

    # -- helpers ----------------------------------------------------------
    def company(self, suffix: str = "Inc.") -> str:
        name = self.fk.last_name()
        if " " in suffix:  # suffix already says what the business does
            return f"{name} {suffix}"
        noun = self.rng.choice(["Analytics", "Logistics", "Foods", "Health", "Robotics", "Media", "Builders",
                                "Energy", "Outfitters", "Labs", "Dental", "Partners", "Systems", "Supply"])
        return f"{name} {noun} {suffix}"

    def bn(self) -> str:
        return f"{self.rng.randint(100000000, 999999999)}RT0001"

    def day(self) -> date:
        return PERIOD_START + timedelta(days=self.rng.randrange((PERIOD_END - PERIOD_START).days + 1))

    def money(self, lo: float, hi: float, step: float = 0.01) -> Decimal:
        n = self.rng.randint(int(lo / step), int(hi / step))
        return r2(Decimal(str(n)) * Decimal(str(step)))

    def add(self, *, d, desc, direction, net, tax=Decimal("0"), bn="", group, category, supply_type,
            place="NOT_APPLICABLE", itc="not_applicable", gross=None, duplicate=False, note="",
            qst=Decimal("0"), qst_no=""):
        gross = r2(net + tax + qst) if gross is None else gross
        self.rows.append(dict(
            date=d, description=desc, amount=gross if direction == "in" else -gross, net=net, gst_hst=tax,
            supplier_bn=bn, qst=qst, supplier_qst=qst_no, group=group, category=category, supply_type=supply_type,
            place_of_supply=place, itc_treatment=itc, is_duplicate=duplicate, note=note))

    # -- the quarter ------------------------------------------------------
    def build(self) -> None:
        rng = self.rng
        customers = {p: [(self.company(rng.choice(["Inc.", "Ltd."])), c) for c in cs] for p, cs in CITIES.items()}

        # Domestic sales: 18 ON, 9 AB, 9 BC. Two descriptions omit the location.
        no_city = {5, 22}
        overcharged = 20  # an AB invoice (AB rows are 18-26) billed at 13% instead of 5%
        i = 0
        for prov, n in (("ON", 18), ("AB", 9), ("BC", 9)):
            for _ in range(n):
                cust, city = rng.choice(customers[prov])
                self.inv += rng.randint(1, 4)
                net = self.money(1200, 9800, 50)
                charged = r2(net * (RATE["ON"] if i == overcharged else RATE[prov]))
                if i in no_city:
                    desc = f"Deposit {cust} INV {self.inv}"
                else:
                    desc = rng.choice([
                        f"Payment received - INV-{self.inv} - {cust} ({city} {prov}) - advisory services",
                        f"e-Transfer deposit from {cust}, {city} {prov} re INV-{self.inv}",
                        f"Wire in INV-{self.inv} {cust} {city} {prov} - consulting retainer",
                    ])
                self.add(d=self.day(), desc=desc, direction="in", net=net, tax=charged,
                         group=f"sale_{prov}" + ("_nocity" if i in no_city else ""),
                         category="sale_domestic", supply_type="taxable", place=prov,
                         note="overcharged 13% on AB customer" if i == overcharged else "")
                i += 1

        # Exports to US clients (zero-rated consulting to non-residents).
        for city, st in (("Austin", "TX"), ("Seattle", "WA")):
            self.inv += 2
            us = f"{self.fk.last_name()} Software LLC"
            self.add(d=self.day(), desc=f"Wire in USD conv. CAD - INV-{self.inv} - {us}, {city} {st} USA - strategy advisory",
                     direction="in", net=self.money(6000, 14000, 100), group="export", category="sale_export",
                     supply_type="zero_rated", place="OUTSIDE_CANADA")

        # Exempt long-term residential rent from a unit the company owns.
        for y, m in MONTHS:
            self.add(d=date(y, m, 1), desc=f"Rent received - residential tenant - 18 Birch Ave Unit 2 Hamilton ON - {date(y, m, 1):%B}",
                     direction="in", net=Decimal("2150.00"), group="rent", category="sale_exempt_residential_rent",
                     supply_type="exempt", place="ON")

        # Credit note to an Ontario customer (large refund).
        cust, city = customers["ON"][0]
        net = Decimal("2400.00")
        self.add(d=date(2026, 9, 18), desc=f"Refund to customer - credit note CN-0007 against INV-4107 - {cust} ({city} ON) - scope reduction",
                 direction="out", net=net, tax=r2(net * RATE["ON"]), group="credit_note", category="credit_note",
                 supply_type="taxable", place="ON")

        # General purchases from Canadian registrants.
        landlord, isp, mobile = self.company("Properties Ltd."), self.company("Networks Inc."), self.company("Mobile Inc.")
        for y, m in MONTHS:
            self._p(date(y, m, 1), f"Office rent - Suite 1200 commercial lease - {landlord}", Decimal("3200.00"), "general", bn=self.bn())
            self._p(date(y, m, 5), f"Telecom - {mobile} business mobile plan", Decimal("186.40"), "general", bn=self.bn())
            self._p(date(y, m, 8), f"Internet - {isp} business fibre", Decimal("95.00"), "general", bn=self.bn())
        supplies = self.company("Office Supply Ltd.")
        supplies_bn = self.bn()
        for k in range(5):
            net = self.money(12, 340)
            small = net + r2(net * RATE["ON"]) < 30
            self._p(self.day(), f"Office supplies - {supplies}", net, "general", bn="" if small else supplies_bn)
        courier = self.company("Couriers Inc.")
        for k in range(4):
            self._p(self.day(), f"Courier - {courier} waybill {rng.randint(100000, 999999)}", self.money(18, 85), "general", bn=self.bn())
        ab_courier = f"{self.fk.last_name()} Freight Ltd. Calgary"
        self._p(self.day(), f"Courier - {ab_courier} waybill {rng.randint(100000, 999999)}", self.money(40, 120), "general",
                bn=self.bn(), prov="AB")
        for k in range(4):
            name = self.fk.name()
            self._p(self.day(), f"Contractor invoice - {name} - design services", self.money(1200, 3800, 25), "general",
                    bn="" if k == 1 else self.bn(), note="no BN on contractor invoice" if k == 1 else "")
        self._p(self.day(), f"Accounting fees - Q2 review - {self.fk.last_name()} CPA LLP", Decimal("1850.00"), "general", bn=self.bn())
        self._p(self.day(), f"Legal fees - contract review - {self.fk.last_name()} LLP", Decimal("975.00"), "general",
                bn="12345678RT001", note="malformed BN")
        sw = f"{self.fk.last_name()}Plan Inc. (Toronto)"
        for y, m in MONTHS:
            self._p(date(y, m, 12), f"Software - {sw} project tool subscription", Decimal("120.00"), "general", bn=self.bn())
        for k in range(2):
            self._p(self.day(), f"Printing - client workshop handouts - {self.fk.last_name()} Print Shop", self.money(60, 260), "general", bn=self.bn())
        for k in range(2):
            self._p(self.day(), "Postage - mailing of signed contracts", self.money(8, 26), "general")
        for k in range(4):
            self._p(self.day(), f"Parking - {self.fk.street_name()} lot - client visit", self.money(9, 28), "general")
        # Totals mismatch: bank shows more than net + tax (unexplained difference).
        net = Decimal("64.00")
        self._p(self.day(), f"Courier - {courier} same-day delivery", net, "general", bn=self.bn(),
                gross=r2(net * Decimal("1.13")) + Decimal("5.00"), note="bank amount includes $5 not on invoice")
        # Duplicate posting of an office supply purchase.
        d = self.day()
        net = Decimal("212.40")
        desc = f"Office supplies - {supplies} - toner"
        self._p(d, desc, net, "general", bn=supplies_bn)
        self._p(d, desc, net, "general_duplicate", bn=supplies_bn, duplicate=True, note="duplicate posting")

        # Ambiguous descriptions (the rules extractor cannot know; gold labels do).
        self._p(self.day(), f"Online marketplace order #ORD-{rng.randint(10000, 99999)}", self.money(40, 160), "ambiguous",
                bn=self.bn(), note="label: printer paper and cables")
        self._p(self.day(), f"Online marketplace order #ORD-{rng.randint(10000, 99999)}", self.money(40, 160), "ambiguous",
                bn=self.bn(), category="personal", itc="none", note="label: kids' toys")
        self._p(self.day(), f"POS purchase SQ *JUNIPER {rng.randint(1000, 9999)}", self.money(32, 90), "ambiguous",
                bn=self.bn(), category="meals_entertainment", itc="restricted_50", note="label: client lunch at a cafe")
        self._p(self.day(), f"POS purchase SQ *HALCYON {rng.randint(1000, 9999)}", self.money(15, 60), "ambiguous",
                note="label: stationery")

        # Meals and entertainment.
        for k in range(10):
            place = f"{self.fk.last_name()}'s " + rng.choice(["Bistro", "Kitchen", "Cafe", "Grill", "Brasserie"])
            desc, lo, hi = rng.choice([
                (f"Client lunch - {place}", 45, 180),
                (f"Team dinner - {place}", 120, 420),
                (f"Coffee meeting - {place}", 8, 24),
                (f"Hockey tickets - client entertainment - {self.fk.city()} Arena", 180, 480),
                (f"Catering - client workshop - {place}", 300, 900),
            ])
            net = self.money(lo, hi)
            gross = net + r2(net * RATE["ON"])
            no_bn = gross < 30 or k == 3
            self._p(self.day(), desc, net, "meals", bn="" if no_bn else self.bn(),
                    category="meals_entertainment", itc="restricted_50",
                    note="no BN on receipt" if k == 3 and gross >= 30 else "")

        # Personal expenses through the business account.
        self._p(self.day(), "Personal - groceries - Harvest Lane Market", self.money(80, 240), "personal",
                category="personal", itc="none", bn=self.bn())
        self._p(date(2026, 9, 3), "Lakeshore Fitness Club monthly membership", Decimal("79.00"), "personal",
                category="personal", itc="none", bn=self.bn())
        self._p(self.day(), f"Personal: family dinner - {self.fk.last_name()}'s Trattoria", self.money(90, 210), "personal",
                category="personal", itc="none", bn=self.bn())

        # Insurance (exempt) and bank fees (exempt).
        insurer = f"{self.fk.last_name()} Mutual Insurance"
        for y, m in MONTHS:
            self.add(d=date(y, m, 15), desc=f"Insurance premium - commercial general liability - {insurer}", direction="out",
                     net=Decimal("265.00"), group="insurance", category="insurance", supply_type="exempt", itc="none")
            self.add(d=date(y, m, 28), desc="Monthly account fee - business chequing", direction="out",
                     net=Decimal("25.00"), group="bank_fees", category="bank_fees", supply_type="exempt", itc="none")
        for k in range(2):
            self.add(d=self.day(), desc="Wire transfer fee - outgoing", direction="out", net=Decimal("15.00"),
                     group="bank_fees", category="bank_fees", supply_type="exempt", itc="none")

        # Foreign digital services: three from a non-registrant, one from a vendor that charged HST.
        us_saas = f"{self.fk.last_name()}Canvas Inc."
        for y, m in MONTHS:
            self.add(d=date(y, m, 20), desc=f"{us_saas} subscription USD 49.00 - San Francisco CA (FX converted)", direction="out",
                     net=Decimal("67.42"), group="foreign", category="foreign_digital_service", supply_type="out_of_scope",
                     place="OUTSIDE_CANADA", itc="none")
        net = Decimal("540.00")
        self.add(d=date(2026, 10, 9), desc=f"Foreign transaction - {self.fk.last_name()}Host cloud hosting annual plan (Dublin, Ireland) incl. HST",
                 direction="out", net=net, tax=r2(net * RATE["ON"]), group="foreign_charged", category="foreign_digital_service",
                 supply_type="out_of_scope", place="OUTSIDE_CANADA", itc="none", note="simplified-registration vendor charged HST")

        # Capital asset.
        self._p(date(2026, 9, 10), f"Purchase of 3 laptops - capital equipment - {self.fk.last_name()} Computers Ltd.",
                Decimal("4650.00"), "capital", bn=self.bn(), category="capital_asset")

        # Out of scope.
        for d, desc, amt, direction in [
            (date(2026, 8, 14), "Transfer to savings account", Decimal("10000.00"), "out"),
            (date(2026, 10, 14), "Transfer to savings account", Decimal("8000.00"), "out"),
            (date(2026, 8, 31), "Receiver General - CRA GST/HST payment - period ending 2026-07-31", Decimal("4318.27"), "out"),
            (date(2026, 8, 29), "Owner draw - transfer to personal account", Decimal("3500.00"), "out"),
            (date(2026, 9, 29), "Owner draw - transfer to personal account", Decimal("3500.00"), "out"),
            (date(2026, 10, 30), "Owner draw - transfer to personal account", Decimal("3500.00"), "out"),
            (date(2026, 8, 3), "Owner contribution - deposit from shareholder", Decimal("5000.00"), "in"),
        ]:
            self.add(d=d, desc=desc, direction=direction, net=amt, group="out_of_scope",
                     category="out_of_scope_transfer", supply_type="out_of_scope")

        self._quebec()

    def _quebec(self) -> None:
        """Quebec activity for the QST worksheet: sales to Quebec clients (GST 5% + QST 9.975%), a
        credit note, and purchases from Quebec suppliers whose QST is refundable (ITR)."""
        q = random.Random(QST_SEED)
        day = lambda: PERIOD_START + timedelta(days=q.randrange((PERIOD_END - PERIOD_START).days + 1))  # noqa: E731
        qst_no = lambda: f"{q.randint(10**9, 10**10 - 1)}TQ0001"  # noqa: E731
        bn = lambda: f"{q.randint(100000000, 999999999)}RT0001"  # noqa: E731
        inv = 4400
        cities = ["Montreal", "Quebec City", "Laval", "Gatineau"]
        clients = [(f"{self.fk.last_name()} {q.choice(['Analytics', 'Logistics', 'Health', 'Media'])} Inc.",
                    q.choice(cities)) for _ in range(3)]
        for k in range(4):
            cust, city = clients[k % 3]
            inv += q.randint(1, 3)
            net = r2(Decimal(q.randint(24, 160) * 50))
            # The last sale is billed with GST only: QST collectible is still owed (flagged).
            qst = Decimal("0") if k == 3 else r2(net * QST_RATE)
            self.add(d=day(), desc=f"Payment received - INV-{inv} - {cust} ({city} QC) - advisory services",
                     direction="in", net=net, tax=r2(net * RATE["QC"]), qst=qst, group="qc_sale",
                     category="sale_domestic", supply_type="taxable", place="QC",
                     note="QST not charged on a Quebec sale" if k == 3 else "")
        cust, city = clients[0]
        net = Decimal("800.00")
        self.add(d=date(2026, 10, 6), desc=f"Refund to customer - credit note CN-0012 against INV-4401 - {cust} ({city} QC)",
                 direction="out", net=net, tax=r2(net * RATE["QC"]), qst=r2(net * QST_RATE), group="qc_credit_note",
                 category="credit_note", supply_type="taxable", place="QC")
        purchases = [
            (f"Contractor invoice - {self.fk.name()} (Montreal QC) - French translation of client deliverables",
             Decimal("1450.00"), "purchase_general", "full", True, ""),
            (f"Printing - {self.fk.last_name()} Imprimerie (Laval QC) - workshop binders",
             Decimal("385.20"), "purchase_general", "full", True, ""),
            (f"Client lunch - {self.fk.last_name()}'s Brasserie, Montreal QC",
             Decimal("164.80"), "meals_entertainment", "restricted_50", True, ""),
            (f"Courier - {self.fk.last_name()} Express (Gatineau QC) waybill {q.randint(100000, 999999)}",
             Decimal("142.00"), "purchase_general", "full", False, "no QST number on courier invoice"),
        ]
        for desc, net, cat, itc, has_qst_no, note in purchases:
            self.add(d=day(), desc=desc, direction="out", net=net, tax=r2(net * RATE["QC"]), bn=bn(),
                     qst=r2(net * QST_RATE), qst_no=qst_no() if has_qst_no else "", group="qc_purchase",
                     category=cat, supply_type="taxable", itc=itc, note=note)

    def _p(self, d, desc, net, group, *, bn="", category="purchase_general", itc=None, prov="ON", gross=None,
           duplicate=False, note=""):
        """A purchase with GST/HST at the vendor's rate."""
        tax = r2(net * RATE[prov])
        itc = itc or ("restricted_50" if category == "meals_entertainment" else
                      "none" if category == "personal" else "full")
        self.add(d=d, desc=desc, direction="out", net=net, tax=tax, bn=bn, group=group, category=category,
                 supply_type="taxable", itc=itc, gross=gross, duplicate=duplicate, note=note)

    # -- outputs ----------------------------------------------------------
    def finalize(self) -> None:
        # Stable order: by date, then insertion order (keeps the duplicate after its original).
        self.rows = [r for _, r in sorted(enumerate(self.rows), key=lambda t: (t[1]["date"], t[0]))]
        for n, r in enumerate(self.rows, 1):
            r["txn_id"] = f"T{n:03d}"
        self._pick_eval_set()

    def _pick_eval_set(self) -> None:
        quota = {"export": 2, "rent": 1, "credit_note": 1, "personal": 3, "insurance": 1, "bank_fees": 2,
                 "foreign": 1, "foreign_charged": 1, "capital": 1, "out_of_scope": 2, "ambiguous": 4,
                 "meals": 3, "general": 3, "sale_ON": 1, "sale_AB": 2, "sale_BC": 1, "sale_ON_nocity": 1}
        assert sum(quota.values()) == 30
        picked = set()
        for g, n in quota.items():
            ids = [r["txn_id"] for r in self.rows if r["group"] == g]
            picked.update(self.rng.sample(ids, n))
        for r in self.rows:
            r["in_eval_set"] = r["txn_id"] in picked

    def expected_gst34(self) -> dict:
        """GST34 lines from the gold labels, worked out directly in this file."""
        cents = lambda d: int((d * 100).to_integral_value())
        l101 = l103 = l106 = l107 = 0
        for r in self.rows:
            net, tax, cat = cents(r["net"]), cents(r["gst_hst"]), r["category"]
            if cat in ("sale_domestic", "sale_export", "sale_exempt_residential_rent"):
                l101 += net
                if r["supply_type"] == "taxable":
                    l103 += max(cents(r2(r["net"] * RATE[r["place_of_supply"]])), tax)
            elif cat == "credit_note":
                l101 -= net
                l107 += tax
            elif not r["is_duplicate"] and r["itc_treatment"] == "full":
                l106 += tax
            elif not r["is_duplicate"] and r["itc_treatment"] == "restricted_50":
                l106 += cents(r2(r["gst_hst"] / 2))
        l109 = l103 - (l106 + l107)
        lines = {"101": l101, "103": l103, "104": 0, "105": l103, "106": l106, "107": l107, "108": l106 + l107,
                 "109": l109, "110": 0, "111": 0, "112": 0, "113A": l109, "205": 0, "405": 0, "113B": 0,
                 "113C": l109, "114": max(-l109, 0), "115": max(l109, 0)}
        return {"unit": "cents", "period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat(),
                "lines": lines}

    def expected_qst(self) -> dict:
        """QST return lines from the gold labels, worked out directly in this file."""
        cents = lambda d: int((d * 100).to_integral_value())  # noqa: E731
        l203 = l206 = l207 = 0
        for r in self.rows:
            qst, cat = cents(r["qst"]), r["category"]
            if cat == "sale_domestic" and r["place_of_supply"] == "QC":
                l203 += max(cents(r2(r["net"] * QST_RATE)), qst)
            elif cat == "credit_note":
                l207 += qst
            elif not r["is_duplicate"] and r["itc_treatment"] == "full":
                l206 += qst
            elif not r["is_duplicate"] and r["itc_treatment"] == "restricted_50":
                l206 += cents(r2(r["qst"] / 2))
        lines = {"203": l203, "204": 0, "205": l203, "206": l206, "207": l207, "208": l206 + l207,
                 "209": l203 - (l206 + l207)}
        return {"unit": "cents", "period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat(),
                "lines": lines}

    def write(self, out: Path = OUT) -> None:
        out.mkdir(parents=True, exist_ok=True)
        business = {
            "name": "Northlake Advisory Inc.", "legal_note": "synthetic business, not a real registrant",
            "bn": f"{self.rng.randint(100000000, 999999999)}RT0001", "province": "ON",
            "email": "books@northlake-advisory.test", "method": "regular", "reporting_frequency": "quarterly",
            "fiscal_year_end": "01-31", "period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat(),
            "instalments_paid": "0.00",
            # QST registration with Revenu Quebec (synthetic number), so the agent also prepares
            # the QST worksheet and claims input tax refunds on QST paid to Quebec suppliers.
            "qst_registered": True,
            "qst_number": f"{random.Random(QST_SEED + 1).randint(10**9, 10**10 - 1)}TQ0001",
        }
        (out / "business.json").write_text(json.dumps(business, indent=2) + "\n")
        with (out / "transactions.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["txn_id", "date", "description", "amount", "net", "gst_hst", "supplier_bn", "qst",
                        "supplier_qst"])
            for r in self.rows:
                w.writerow([r["txn_id"], r["date"].isoformat(), r["description"], f"{r['amount']:.2f}",
                            f"{r['net']:.2f}", f"{r['gst_hst']:.2f}", r["supplier_bn"], f"{r['qst']:.2f}",
                            r["supplier_qst"]])
        with (out / "labels.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["txn_id", "category", "supply_type", "place_of_supply", "itc_treatment", "is_duplicate",
                        "in_eval_set", "group", "note"])
            for r in self.rows:
                w.writerow([r["txn_id"], r["category"], r["supply_type"], r["place_of_supply"], r["itc_treatment"],
                            int(r["is_duplicate"]), int(r["in_eval_set"]), r["group"], r["note"]])
        (out / "expected_gst34.json").write_text(json.dumps(self.expected_gst34(), indent=2) + "\n")
        (out / "expected_qst.json").write_text(json.dumps(self.expected_qst(), indent=2) + "\n")


def main() -> None:
    g = Gen(42)
    g.build()
    g.finalize()
    g.write()
    print(f"wrote {len(g.rows)} transactions to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
