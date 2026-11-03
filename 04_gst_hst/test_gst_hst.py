import copy
import csv
import json
from datetime import date
from pathlib import Path

import pytest

import gst_hst_data
from gst_hst import (DATA_DIR, Classification, Rules, Txn, add_one_month, compute_gst34, cra_holidays,
                     filing_deadline, gold_classifications, load_labels, load_transactions, mul_round, to_cents,
                     treat)
from gst_hst_extract import SCHEMA, ClaudeExtractor, RulesExtractor, rules_for_prompt
from shared import llm

RULES = Rules()


def txn(desc, amount, net=None, tax=0, bn="", tid="X1", d=date(2026, 9, 1)):
    amount = to_cents(amount)
    net = abs(amount) - to_cents(tax) if net is None else to_cents(net)
    return Txn(tid, d, desc, amount, net, to_cents(tax), bn)


def cls(category, place="NOT_APPLICABLE", conf=0.95, rules=RULES):
    c = rules.category(category)
    return Classification(category, c["supply_type"], place, c["itc_treatment"], conf)


def run_one(t, c, rules=RULES):
    return treat([t], {t.txn_id: c}, rules)[0]


# --------------------------------------------------------------------------- money

def test_to_cents_exact():
    assert to_cents("1,234.56") == 123456
    assert to_cents("-0.005") == -1  # half away from zero
    assert to_cents("") == 0


def test_mul_round_half_up():
    assert mul_round(25, 0.5) == 13          # 12.5 -> 13
    assert mul_round(1, 0.5) == 1
    assert mul_round(-25, 0.5) == -13
    assert mul_round(999, 0.05) == 50        # matches shared.tax rounding


# --------------------------------------------------------------------------- rules file

def test_rules_file_loads_and_is_cited():
    assert RULES.home == "ON"  # YAML must not read ON as boolean True
    for name, c in RULES.categories.items():
        assert c["cite"], f"{name} has no citation"
    for k, s in RULES.raw["sources"].items():
        assert s["url"].startswith("https://"), k


@pytest.mark.parametrize("text,place", [
    ("Wagner Labs Ltd. (Kelowna BC)", "BC"),
    ("Miller Health, Edmonton", "AB"),
    ("deposit on account", "UNKNOWN"),          # lowercase 'on' is not Ontario
    ("Software LLC, Austin TX USA", "OUTSIDE_CANADA"),
    ("Hamilton ON", "ON"),
])
def test_place_of_supply_tokens(text, place):
    assert RULES.place_from_text(text) == place


# --------------------------------------------------------------------------- rules extractor

@pytest.mark.parametrize("desc,amount,category", [
    ("Personal: family dinner - Rossi's Trattoria", "-150", "personal"),           # personal beats meals
    ("Owner draw - transfer to personal account", "-3500", "out_of_scope_transfer"),  # transfer beats personal
    ("Refund to customer - credit note CN-0007 against INV-4107 (Toronto ON)", "-2712", "credit_note"),
    ("Wire in USD conv. CAD - INV-4190 - Acme LLC, Seattle WA USA", "8900", "sale_export"),
    ("Rent received - residential tenant - 18 Birch Ave Hamilton ON", "2150", "sale_exempt_residential_rent"),
    ("Payment received - INV-4186 - Wagner Labs (Kelowna BC)", "9135", "sale_domestic"),
    ("Insurance premium - commercial general liability", "-265", "insurance"),
    ("Wire transfer fee - outgoing", "-15", "bank_fees"),
    ("CloudCo subscription USD 49.00 - San Francisco CA", "-67.42", "foreign_digital_service"),
    ("Purchase of 3 laptops - capital equipment - Byte Computers", "-5254.50", "capital_asset"),
    ("Client lunch - Harbour Bistro", "-90", "meals_entertainment"),
    ("Office rent - Suite 1200 commercial lease", "-3616", "purchase_general"),
    ("Receiver General - CRA GST/HST payment - period ending 2026-07-31", "-4318.27", "out_of_scope_transfer"),
])
def test_rules_extractor_categories(desc, amount, category):
    c = RulesExtractor(RULES).classify(txn(desc, amount))
    assert c.category == category
    assert c.confidence >= RULES.t["low_confidence"]


def test_rules_extractor_fallback_is_low_confidence():
    c = RulesExtractor(RULES).classify(txn("POS purchase SQ *JUNIPER 1958", "-89.77"))
    assert c.category == "purchase_general" and c.confidence < RULES.t["low_confidence"]
    t = run_one(txn("POS purchase SQ *JUNIPER 1958", "-89.77", tax="10.33"), c)
    assert any(f.startswith("low confidence") for f in t.flags)


def test_direction_filters_categories():
    # Money in can never be a purchase category, whatever the words say.
    c = RulesExtractor(RULES).classify(txn("Deposit - office supplies refund from vendor", "40"))
    assert RULES.category(c.category)["direction"] in ("in", "any")


# --------------------------------------------------------------------------- engine: ITCs

def test_meals_50_percent_itc_rounds_half_up():
    t = run_one(txn("Client lunch", "-113.25", net="100.22", tax="13.03", bn="123456789RT0001"),
                cls("meals_entertainment"))
    assert t.lines["106"] == 652  # 13.03 / 2 = 6.515 -> 6.52


def test_personal_gets_no_itc_and_is_flagged():
    t = run_one(txn("Personal - groceries", "-113", tax="13", bn="123456789RT0001"), cls("personal"))
    assert "106" not in t.lines
    assert any("personal" in f for f in t.flags)


def test_exempt_inputs_no_itc():
    for cat in ("insurance", "bank_fees"):
        t = run_one(txn("fee", "-25"), cls(cat))
        assert t.lines == {} and not t.flags


def test_tax_on_exempt_is_flagged():
    t = run_one(txn("Insurance premium", "-113", tax="13"), cls("insurance"))
    assert "106" not in t.lines and any("treat as exempt" in f for f in t.flags)


def test_capital_asset_full_itc_and_flag():
    t = run_one(txn("laptops", "-5254.50", tax="604.50", bn="123456789RT0001"), cls("capital_asset"))
    assert t.lines["106"] == 60450 and any("capital" in f for f in t.flags)


@pytest.mark.parametrize("gross,tax,expect", [
    ("29.99", "3.45", None),        # under $30: nothing required
    ("30.00", "3.45", "advisory"),  # $30-$99.99: flagged, advisory
    ("100.00", "11.50", "required"),
])
def test_missing_bn_threshold(gross, tax, expect):
    t = run_one(txn("Office supplies", f"-{gross}", tax=tax), cls("purchase_general"))
    bn_flags = [f for f in t.flags if "registration number" in f]
    if expect is None:
        assert not bn_flags
    else:
        assert len(bn_flags) == 1 and expect in bn_flags[0]


def test_malformed_bn_flagged():
    t = run_one(txn("Legal fees", "-1101.75", tax="126.75", bn="12345678RT001"), cls("purchase_general"))
    assert any("not 9 digits" in f for f in t.flags)


def test_purchase_rate_not_a_canadian_rate_flagged():
    t = run_one(txn("Office supplies", "-110", tax="10", bn="123456789RT0001"), cls("purchase_general"))
    assert any("rate mismatch" in f for f in t.flags)


def test_totals_mismatch_flagged():
    t = run_one(txn("Courier", "-77.32", net="64.00", tax="8.32", bn="123456789RT0001"), cls("purchase_general"))
    assert any("totals mismatch" in f for f in t.flags)


def test_foreign_digital_flags_and_no_itc():
    t = run_one(txn("Host plan Dublin Ireland incl. HST", "-610.20", tax="70.20"),
                cls("foreign_digital_service", "OUTSIDE_CANADA"))
    assert "106" not in t.lines and "405" not in t.lines
    assert any("211.17" in f for f in t.flags) and any("foreign digital" in f for f in t.flags)


def test_foreign_digital_self_assessed_when_not_exclusively_commercial():
    rules = copy.deepcopy(RULES)
    rules.categories["foreign_digital_service"]["default_commercial_use_pct"] = 60
    t = run_one(txn("CloudCo subscription USD", "-100.00"), cls("foreign_digital_service", "OUTSIDE_CANADA", rules=rules),
                rules)
    assert t.lines["405"] == 1300


# --------------------------------------------------------------------------- engine: sales

def test_sale_tax_by_place_of_supply():
    for prov, tax in (("ON", 1300), ("AB", 500), ("BC", 500)):
        t = run_one(txn("INV-1", f"{100 + tax / 100:.2f}", tax=f"{tax / 100:.2f}"), cls("sale_domestic", prov))
        assert t.lines == {"101": 10000, "103": tax} and not t.flags


def test_overcollected_tax_is_reported_as_collected():
    t = run_one(txn("INV-2 Edmonton AB", "113", tax="13"), cls("sale_domestic", "AB"))
    assert t.lines["103"] == 1300 and any("225(1)" in f for f in t.flags)


def test_undercollected_tax_reports_collectible():
    t = run_one(txn("INV-3 Toronto ON", "105", tax="5"), cls("sale_domestic", "ON"))
    assert t.lines["103"] == 1300 and any("rate mismatch" in f for f in t.flags)


def test_unknown_place_defaults_home_and_flags():
    t = run_one(txn("Deposit INV 9", "113", tax="13"), cls("sale_domestic", "UNKNOWN"))
    assert t.place == "ON" and any("assumed" in f for f in t.flags)


def test_export_and_exempt_rent_on_line_101_only():
    e = run_one(txn("Wire in USD", "8900"), cls("sale_export", "OUTSIDE_CANADA"))
    r = run_one(txn("Rent received residential", "2150"), cls("sale_exempt_residential_rent", "ON"))
    assert e.lines == {"101": 890000} and r.lines == {"101": 215000}


def test_credit_note_reduces_101_and_goes_to_107():
    t = run_one(txn("credit note CN-7", "-2712", tax="312"), cls("credit_note", "ON"))
    assert t.lines == {"101": -240000, "107": 31200}
    assert any("large refund" in f for f in t.flags)
    small = run_one(txn("credit note CN-8", "-113", tax="13"), cls("credit_note", "ON"))
    assert not any("large refund" in f for f in small.flags)


def test_duplicate_held_out():
    a = txn("Office supplies - toner", "-240.01", tax="27.61", bn="123456789RT0001", tid="A")
    b = txn("Office supplies - toner", "-240.01", tax="27.61", bn="123456789RT0001", tid="B")
    out = treat([a, b], {"A": cls("purchase_general"), "B": cls("purchase_general")}, RULES)
    assert out[0].lines["106"] == 2761 and out[1].held_out and out[1].lines == {}
    assert compute_gst34(out)["106"] == 2761


def test_rule_conflict_flagged_and_rules_win():
    c = Classification("meals_entertainment", "taxable", "NOT_APPLICABLE", "full", 0.9)
    t = run_one(txn("Client lunch", "-113", tax="13", bn="123456789RT0001"), c)
    assert t.itc_treatment == "restricted_50" and t.lines["106"] == 650
    assert any("rule conflict" in f for f in t.flags)


# --------------------------------------------------------------------------- GST34

def test_gst34_refund_position():
    sale = txn("INV-1 Toronto ON", "1130", tax="130", tid="S")
    cap = txn("server purchase", "-11300", tax="1300", bn="123456789RT0001", tid="C")
    out = treat([sale, cap], {"S": cls("sale_domestic", "ON"), "C": cls("capital_asset")}, RULES)
    L = compute_gst34(out)
    assert L["109"] == 13000 - 130000 and L["114"] == 117000 and L["115"] == 0


def test_gst34_identities_with_instalments():
    sale = txn("INV-1 Toronto ON", "11300", tax="1300", tid="S")
    L = compute_gst34(treat([sale], {"S": cls("sale_domestic", "ON")}, RULES), instalments_cents=50000)
    assert L["105"] == L["103"] + L["104"]
    assert L["113A"] == L["109"] - L["112"] == 130000 - 50000
    assert L["113C"] == L["113A"] + L["113B"] and L["115"] == 80000


def test_gold_labels_reproduce_expected_lines():
    """The engine, given the gold labels, matches the generator's independent line totals exactly."""
    txns = load_transactions()
    L = compute_gst34(treat(txns, gold_classifications(load_labels()), RULES))
    expected = json.loads((DATA_DIR / "expected_gst34.json").read_text())["lines"]
    assert L == expected


# --------------------------------------------------------------------------- deadline

@pytest.mark.parametrize("end,due,eff", [
    (date(2026, 10, 31), date(2026, 11, 30), date(2026, 11, 30)),
    (date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 2)),   # Saturday
    (date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 1)),    # Sunday, year rollover
    (date(2026, 3, 31), date(2026, 4, 30), date(2026, 4, 30)),
    (date(2027, 2, 28), date(2027, 3, 31), date(2027, 3, 31)),    # month-end to month-end
    (date(2026, 3, 3), date(2026, 4, 3), date(2026, 4, 7)),       # Good Friday + weekend + Easter Monday
    (date(2026, 7, 3), date(2026, 8, 3), date(2026, 8, 4)),       # Civic Holiday
])
def test_filing_deadline(end, due, eff):
    assert filing_deadline(end) == (due, eff)


def test_add_one_month_clamps():
    assert add_one_month(date(2026, 1, 30)) == date(2026, 2, 28)


def test_holidays_2026_match_cra_list():
    h = cra_holidays(2026)
    for d in [date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 18), date(2026, 8, 3), date(2026, 9, 7),
              date(2026, 9, 30), date(2026, 10, 12), date(2026, 11, 11)]:
        assert d in h
    assert date(2026, 6, 24) not in h and date(2026, 6, 24) in cra_holidays(2026, "QC")


# --------------------------------------------------------------------------- data, Claude wiring, run

def test_generator_is_reproducible(tmp_path):
    g = gst_hst_data.Gen(42)
    g.build()
    g.finalize()
    g.write(tmp_path)
    for name in ("transactions.csv", "labels.csv", "expected_gst34.json", "business.json"):
        assert (tmp_path / name).read_text() == (DATA_DIR / name).read_text(), name


def test_eval_set_size_and_no_labels_in_input():
    labels = load_labels()
    assert sum(int(r["in_eval_set"]) for r in labels.values()) == 30
    header = (DATA_DIR / "transactions.csv").read_text().splitlines()[0]
    assert "category" not in header and "supply_type" not in header


def test_claude_extractor_schema_and_prompt():
    ex = ClaudeExtractor(RULES)
    assert ex.schema["properties"]["category"]["enum"] == list(RULES.categories)
    assert SCHEMA["properties"]["category"]["enum"] is None  # template untouched
    for name in RULES.categories:
        assert name in ex.system
    assert "keywords" not in rules_for_prompt(RULES)
    p = json.loads(ex.prompt(txn("Client lunch", "-113", tax="13")))
    assert p["direction"] == "money out" and "category" not in p


def test_claude_extractor_replay_without_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(llm.FixtureMissing):
        ClaudeExtractor(RULES, fixtures_dir=tmp_path).classify(txn("Client lunch", "-113", tax="13"))


def test_run_dry_and_auto(tmp_path, monkeypatch):
    # Load by path: every module has a run.py, so a bare 'import run' could pick up another one.
    import importlib.util
    spec = importlib.util.spec_from_file_location("gst_hst_run", Path(__file__).with_name("run.py"))
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    monkeypatch.delenv("AFO_LLM_MODE", raising=False)
    assert run.main(["--out", str(tmp_path / "dry"), "--as-of", "2026-11-03"]) == 0
    approvals = json.loads((tmp_path / "dry" / "approvals.json").read_text())
    assert all(p["decision"] == "pending" for p in approvals)
    ret = [p for p in approvals if p["kind"] == "gst_hst_return"]
    assert len(ret) == 1 and ret[0]["payload"]["due_date"] == "2026-11-30"

    # Working-paper CSV traces add up to the return lines.
    gst34 = json.loads((tmp_path / "dry" / "gst34.json").read_text())["lines"]
    with (tmp_path / "dry" / "working_paper.csv").open() as f:
        rows = list(csv.DictReader(f))
    for k in ("101", "103", "106", "107", "405"):
        assert sum(to_cents(r[f"line_{k}"]) for r in rows) == gst34[k]

    assert run.main(["--out", str(tmp_path / "auto"), "--auto-approve"]) == 0
    approvals = json.loads((tmp_path / "auto" / "approvals.json").read_text())
    assert all(p["decision"] == "held" for p in approvals if p["flags"])
    status = json.loads((tmp_path / "auto" / "status.json").read_text())
    assert status["filed"] is False and status["ready_to_file"] is False  # flagged return is held
