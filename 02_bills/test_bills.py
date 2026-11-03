import json
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bills  # noqa: E402
from bills import (  # noqa: E402
    BillReview, bn_check_digit_ok, bn_format_ok, bucket, due_date, find_duplicate, itc_assessment,
    parse_terms, process, tax_rate_check, totals_match,
)
from bills_extract import (  # noqa: E402
    ClaudeExtractor, Extraction, RulesExtractor, categorize, from_model_output, get_extractor, parse_date,
    to_cents,
)
from shared import ledger  # noqa: E402
from shared.approvals import ApprovalQueue  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

DATA = HERE.parent / "data" / "synthetic" / "bills"
AS_OF = date(2026, 11, 3)


def bill(**kw) -> Extraction:
    base = dict(doc_type="bill", vendor_name="Test Vendor Inc.", vendor_province="ON", vendor_country="CA",
                gst_number="123456782RT0001", invoice_number="A-1", invoice_date="2026-10-15",
                subtotal_cents=10000, tax_cents=1300, total_cents=11300, terms="Net 30",
                recipient_name="Tamarack Test Studio Inc.", description="widgets", category="office")
    base.update(kw)
    return Extraction(**base)


# --- GST/HST number --------------------------------------------------------------------------

@pytest.mark.parametrize("bn,ok", [
    ("123456782RT0001", True), ("123456782 RT 0001", True), ("123-456-782-RT-0001", True),
    ("123456782rt0001", True), ("12345678RT0001", False), ("123456782RT001", False),
    ("123456782RC0001", False), ("", False), (None, False),
])
def test_bn_format(bn, ok):
    assert bn_format_ok(bn) is ok


def test_bn_check_digit():
    assert bn_check_digit_ok("123456782RT0001")
    assert not bn_check_digit_ok("123456781RT0001")
    assert not bn_check_digit_ok("12345678RT0001")  # wrong format never passes


def test_generated_bns_pass_check_digit():
    labels = json.loads((DATA / "labels.json").read_text())["items"]
    good = [x["gst_number"] for x in labels if x.get("gst_number") and bn_format_ok(x["gst_number"])]
    assert good and all(bn_check_digit_ok(b) for b in good)


# --- totals and tax rates ----------------------------------------------------------------------

def test_totals_match_with_pst_and_tolerance():
    assert totals_match(bill())
    assert totals_match(bill(total_cents=11301))               # one cent of line rounding
    assert not totals_match(bill(total_cents=11302))
    assert totals_match(bill(tax_cents=500, other_tax_cents=700, total_cents=11200))
    assert not totals_match(bill(subtotal_cents=None))


@pytest.mark.parametrize("vp,country,sub,tax,ok", [
    ("ON", "CA", 10000, 1300, True),
    ("NS", "CA", 90000, 13500, False),   # 15% from NS (14%) to ON (13%)
    ("NS", "CA", 90000, 12600, True),    # NS rate
    ("AB", "CA", 22000, 1100, True),     # vendor-province GST
    ("AB", "CA", 22000, 2860, True),     # recipient-province HST (place of supply)
    ("AB", "CA", 999, 50, True),         # half-cent rounds up
    ("ON", "CA", 10000, 0, True),        # zero-rated / small supplier
    (None, "US", 29900, 0, True),
    (None, "US", 29900, 3887, False),    # non-resident charging tax: confirm registration
    (None, None, 10000, 1300, True),     # unknown province but matches recipient
    (None, None, 10000, 1500, False),
])
def test_tax_rate_check(vp, country, sub, tax, ok):
    e = bill(vendor_province=vp, vendor_country=country, subtotal_cents=sub, tax_cents=tax)
    assert tax_rate_check(e, "ON")[0] is ok


# --- ITC documentary tiers (SOR/91-45 s. 3) ----------------------------------------------------

def test_itc_under_100_needs_no_bn():
    e = bill(gst_number=None, subtotal_cents=8849, tax_cents=1150, total_cents=9999)
    r = itc_assessment(e, "office")
    assert r.status == "eligible" and r.tier == "under $100" and r.claimable_cents == 1150


def test_itc_at_100_needs_bn():
    e = bill(gst_number=None, subtotal_cents=8850, tax_cents=1150, total_cents=10000)
    r = itc_assessment(e, "office")
    assert r.status == "not_eligible" and "valid GST/HST registration number" in r.missing


def test_itc_malformed_bn_is_not_enough():
    assert itc_assessment(bill(gst_number="87654321RT001"), "office").status == "not_eligible"


def test_itc_below_500_does_not_need_terms_or_recipient():
    e = bill(subtotal_cents=44247, tax_cents=5752, total_cents=49999, terms=None, recipient_name=None)
    assert itc_assessment(e, "office").status == "eligible"


def test_itc_at_500_needs_recipient_terms_description():
    e = bill(subtotal_cents=44248, tax_cents=5752, total_cents=50000, terms=None, recipient_name=None,
             description=None)
    r = itc_assessment(e, "office")
    assert r.status == "not_eligible" and r.tier == "$500 or more"
    assert set(r.missing) == {"recipient name", "terms of payment", "description of supply"}


def test_itc_meals_half_rounds_up():
    r = itc_assessment(bill(tax_cents=4941, subtotal_cents=38000, total_cents=42941), "meals")
    assert r.claimable_cents == 2471


def test_itc_not_applicable_without_tax_and_foreign_currency():
    assert itc_assessment(bill(tax_cents=0, total_cents=10000), "office").status == "not_applicable"
    assert itc_assessment(bill(currency="USD"), "office").status == "not_eligible"


def test_itc_held_when_rate_is_wrong():
    e = bill(vendor_province="NS", subtotal_cents=90000, tax_cents=13500, total_cents=103500)
    r = process([("x", e)], as_of=AS_OF)[0]
    assert r.itc.status == "needs_review" and r.itc.claimable_cents == 0


# --- terms, due dates, buckets ----------------------------------------------------------------

@pytest.mark.parametrize("terms,expected", [
    ("Net 30", (30, None, False)), ("NET30", (30, None, False)), ("Due on receipt", (0, None, False)),
    ("Due upon receipt", (0, None, False)), ("2/10 Net 30", (30, 10, False)),
    ("Payment due within 14 days", (14, None, False)), ("Net EOM", ("EOM", None, False)),
    (None, (30, None, True)), ("see contract", (30, None, True)),
])
def test_parse_terms(terms, expected):
    assert parse_terms(terms) == expected


def test_due_dates():
    d = date(2026, 10, 27)
    assert due_date(d, "2/10 Net 30") == (date(2026, 11, 26), date(2026, 11, 6), False)
    assert due_date(d, "Net EOM")[0] == date(2026, 10, 31)
    assert due_date(date(2026, 2, 10), "EOM")[0] == date(2026, 2, 28)
    assert due_date(date(2026, 12, 15), "Net 30")[0] == date(2027, 1, 14)   # crosses year end


@pytest.mark.parametrize("offset,expected", [
    (-1, "overdue"), (0, "due this week"), (7, "due this week"), (8, "next 30 days"),
    (30, "next 30 days"), (31, "later"),
])
def test_bucket_boundaries(offset, expected):
    from datetime import timedelta
    assert bucket(AS_OF + timedelta(days=offset), AS_OF) == expected


# --- duplicates --------------------------------------------------------------------------------

def test_exact_duplicate_ignores_formatting():
    a = bill(vendor_name="Fowler Cloud Software Inc.", invoice_number="CS-20481")
    b = bill(vendor_name="FOWLER CLOUD SOFTWARE", invoice_number="cs 20481", total_cents=99999)
    assert find_duplicate(b, [("a", a)]) == ("exact", "a")


def test_fuzzy_duplicate_window():
    a = bill(invoice_number="CL-1043", invoice_date="2026-10-31", total_cents=29380)
    assert find_duplicate(bill(invoice_number="CL-1044", invoice_date="2026-11-01", total_cents=29380),
                          [("a", a)]) == ("fuzzy", "a")
    assert find_duplicate(bill(invoice_number="CL-1044", invoice_date="2026-11-08", total_cents=29380),
                          [("a", a)]) is None   # 8 days apart
    assert find_duplicate(bill(invoice_number="CL-1044", invoice_date="2026-11-01", total_cents=30000),
                          [("a", a)]) is None   # amount differs by more than 1%
    assert find_duplicate(bill(vendor_name="Other Supplier Ltd.", invoice_number="CL-1043"),
                          [("a", a)]) is None


# --- extraction helpers ------------------------------------------------------------------------

@pytest.mark.parametrize("s,iso", [
    ("2026-10-15", "2026-10-15"), ("October 5, 2026", "2026-10-05"), ("Oct. 5 2026", "2026-10-05"),
    ("5 Oct 2026", "2026-10-05"), ("10/05/2026", None), ("2026-02-30", None), (None, None),
])
def test_parse_date(s, iso):
    assert parse_date(s) == iso


def test_to_cents_and_categories():
    assert to_cents("1,234.50") == 123450 and to_cents("$0.05") == 5 and to_cents("abc") is None
    assert categorize("Miles Office Supply Printer paper") == "office"
    assert categorize("Brown Print Shop brochures") == "advertising"
    assert categorize("unknown thing") == "general"


def test_from_model_output():
    e = from_model_output({"doc_type": "bill", "vendor_name": "X Ltd.", "gst_number": "123456782 RT0001",
                           "invoice_number": "9", "invoice_date": "2026-10-01", "subtotal": "100",
                           "gst_hst": "13.00", "other_tax": None, "total": "113.00", "currency": "cad",
                           "terms": "Net 30", "category": "not-a-category"})
    assert (e.subtotal_cents, e.tax_cents, e.other_tax_cents, e.total_cents) == (10000, 1300, 0, 11300)
    assert e.gst_number == "123456782RT0001" and e.currency == "CAD" and e.category is None
    assert from_model_output({"doc_type": "invoice?"}).doc_type == "other"


def test_get_extractor_follows_llm_mode(monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    assert isinstance(get_extractor(), RulesExtractor)
    assert isinstance(get_extractor("claude"), ClaudeExtractor)
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    assert isinstance(get_extractor(), ClaudeExtractor)
    assert isinstance(get_extractor("rules"), RulesExtractor)


def test_claude_extractor_has_no_invented_fixtures(monkeypatch):
    from shared.llm import FixtureMissing
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(FixtureMissing):
        ClaudeExtractor().extract(read_mailbox(DATA / "inbox")[0])


# --- end to end on the synthetic mailbox -------------------------------------------------------

@pytest.fixture(scope="module")
def mailbox():
    meta = json.loads((DATA / "labels.json").read_text())
    ex = RulesExtractor()
    extracted = [(m.path.name, ex.extract(m)) for m in read_mailbox(DATA / "inbox")]
    return meta, extracted


def test_templated_mail_flags(mailbox):
    meta, extracted = mailbox
    labels = {x["file"]: x for x in meta["items"]}
    reviews = {r.source: r for r in process(extracted, as_of=AS_OF)}
    for src, lab in labels.items():
        if lab["split"] != "templated" or lab["doc_type"] != "bill":
            continue
        got = " | ".join(reviews[src].flags)
        for f in lab["expected_flags"]:
            needle = {"duplicate": "duplicate: same vendor", "possible_duplicate": "possible duplicate",
                      "total_mismatch": "total mismatch", "tax_rate": "tax rate inconsistent",
                      "foreign_currency": "foreign currency", "missing_bn": "missing GST/HST registration",
                      "invalid_bn": "not 9 digits"}[f]
            assert needle in got, (src, f, got)
        if not lab["expected_flags"]:
            assert not reviews[src].flags, (src, reviews[src].flags)


def test_approval_posts_clean_bills_and_rerun_catches_them(mailbox, tmp_path):
    _, extracted = mailbox
    con = ledger.connect(tmp_path / "l.db")
    reviews = process(extracted, as_of=AS_OF)
    q = ApprovalQueue()
    bills.propose_all(q, reviews)
    approved = q.review(auto=True)
    assert approved and all(not p.flags for p in approved)
    assert all(p.decision == "held" for p in q.items if p.flags)
    ids = bills.post_approved(con, [p.payload for p in approved])
    assert con.execute("select count(*) from bills").fetchone()[0] == len(ids)
    again = process(extracted, as_of=AS_OF, existing=bills.ledger_bills(con))
    posted = {p.payload["source"] for p in approved}
    assert all(any("duplicate" in f for f in r.flags) for r in again if r.source in posted)


def test_interactive_reject(mailbox):
    _, extracted = mailbox
    q = ApprovalQueue()
    bills.propose_all(q, process(extracted, as_of=AS_OF))
    assert q.review(ask=lambda _: "n") == []


def test_aging_outputs_exclude_exact_duplicates(mailbox, tmp_path):
    _, extracted = mailbox
    reviews: list[BillReview] = process(extracted, as_of=AS_OF)
    csv_path, md_path = bills.write_aging(reviews, {}, tmp_path, AS_OF)
    rows = csv_path.read_text().splitlines()
    assert len(rows) == len(reviews) + 1
    totals = bills.bucket_totals(reviews)
    dup_total = sum(r.e.total_cents or 0 for r in reviews if any(f.startswith("duplicate:") for f in r.flags))
    all_cad = sum(r.e.total_cents or 0 for r in reviews if r.e.currency == "CAD" and r.bucket)
    assert sum(totals["CAD"].values()) == all_cad - dup_total
    assert "USD" in totals and "AP aging summary" in md_path.read_text()


def test_eval_reports_numbers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval_bills", HERE.parent / "evals" / "eval_bills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = mod.run()["rules"]
    assert res["all"]["messages"] >= 15
    assert 0 <= res["all"]["classification_accuracy"] <= 1
