"""Tests for the deterministic core of the bookkeeping module: parsing, matching, flags,
evidence filing, approvals and the seeded data generator. No model calls."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import bookkeeping as bk  # noqa: E402
import bookkeeping_data  # noqa: E402
import bookkeeping_extract as bx  # noqa: E402
from bookkeeping_match import (Receipt, Txn, amount_ok, days_apart, find_duplicate_charges,  # noqa: E402
                               find_duplicate_receipts, match, merchant_similarity, norm_tokens)
from shared import ledger, llm  # noqa: E402

D = date(2026, 10, 10)


def R(rid="R1", merchant="Blue Door Cafe", day=D, total=1102, cur="CAD", **kw):
    return Receipt(rid, merchant, day, total, cur, **kw)


def T(tid="T1", desc="SQ *BLUE DOOR CAFE TORONTO ON", day=D, amount=-1102, account="VISA-4421"):
    return Txn(tid, day, account, desc, amount)


@pytest.fixture(scope="module")
def chart():
    return bk.load_chart()


# ---------------------------------------------------------------- money parsing

@pytest.mark.parametrize("s,c", [("-2712.00", -271200), ("1,234.56", 123456), ("(12.00)", -1200), ("5", 500),
                                 ("0.5", 50), ("-0.01", -1), ("$19.95", 1995)])
def test_parse_cents(s, c):
    assert bk.parse_cents(s) == c


def test_to_cents_avoids_float_drift():
    assert bk.to_cents(0.1 + 0.2) == 30
    assert bk.to_cents(2145.87) == 214587
    assert bk.to_cents(None) == 0


# ---------------------------------------------------------------- names

def test_norm_tokens_strips_processors_refs_and_store_numbers():
    assert norm_tokens("SQ *BLUE DOOR CAFE TORONTO ON") == ["blue", "door"]
    assert norm_tokens("KESTREL MKTP CA*2K4L19RT3") == ["kestrel"]
    assert norm_tokens("TST* NORTHBOUND COFFEE #12") == ["northbound"]
    assert norm_tokens("INTERAC E-TRF SENT J DOYLE") == ["doyle"]


def test_similarity_handles_truncation_and_numbered_companies():
    assert merchant_similarity("Fennel & Ash Bistro", "FENNEL & ASH BISTRO TORONT") == 1.0
    assert merchant_similarity("Kingsway Properties", "PREAUTH DEBIT KINGSWAY PROP") >= 0.6
    assert merchant_similarity("Lumen Lighting Studio", "2468013 ONTARIO INC") < 0.6
    assert merchant_similarity("Blue Door Cafe", "NORTHLINE FUEL #4412") < 0.6


# ---------------------------------------------------------------- amounts and dates

def test_amount_exact_and_rounding_tolerance():
    assert amount_ok(R(total=1102), T(amount=-1102))[0]
    assert amount_ok(R(total=1102), T(amount=-1104))[0]          # 2 cents
    assert not amount_ok(R(total=1102), T(amount=-1105))[0]
    assert amount_ok(R(total=500000), T(amount=-500900))[0]      # 0.2% of a large amount


def test_refund_needs_money_back():
    refund = R(total=-3276)
    assert amount_ok(refund, T(amount=3276))[0]
    assert not amount_ok(refund, T(amount=-3276))[0]
    assert not amount_ok(R(total=3276), T(amount=3276))[0]


def test_fx_rate_band():
    usd = R(total=4900, cur="USD")
    ok, _, flags = amount_ok(usd, T(amount=-6981))
    assert ok and "1.4247" in flags[0]
    assert not amount_ok(usd, T(amount=-4900))[0]                # 1.00: not converted
    assert not amount_ok(usd, T(amount=-7500))[0]                # 1.53: outside band
    assert not amount_ok(R(total=0, cur="USD"), T(amount=-100))[0]


def test_days_apart_uses_scheduled_debit_date():
    bill = R(day=date(2026, 10, 8), payment_date=date(2026, 10, 16))
    assert days_apart(bill, T(day=date(2026, 10, 16))) == 0
    assert days_apart(R(day=date(2026, 10, 8)), T(day=date(2026, 10, 16))) == 8


def test_date_window_is_five_days():
    assert match([R()], [T(day=date(2026, 10, 15))]).links
    res = match([R()], [T(day=date(2026, 10, 16))])
    assert not res.links and res.orphan_receipts == ["R1"]


# ---------------------------------------------------------------- matching

def test_same_amount_resolved_by_date():
    rs = [R("R1", day=date(2026, 10, 2)), R("R2", day=date(2026, 10, 15))]
    ts = [T("T1", day=date(2026, 10, 2)), T("T2", day=date(2026, 10, 16))]
    links = {(l.receipt_id, l.txn_ids[0]) for l in match(rs, ts).links}
    assert links == {("R1", "T1"), ("R2", "T2")}


def test_split_payment_two_and_three_parts():
    inv = R("R9", "Doyle Studio", date(2026, 10, 6), 282500)
    ts = [T("T1", "INTERAC E-TRF SENT J DOYLE", date(2026, 10, 9), -150000),
          T("T2", "INTERAC E-TRF SENT J DOYLE", date(2026, 10, 23), -132500)]
    link = match([inv], ts).links[0]
    assert link.kind == "split" and link.txn_ids == ["T1", "T2"] and "split payment" in link.flags[0]
    ts3 = [T("T1", "BILL PMT DOYLE", date(2026, 10, 7), -100000), T("T2", "BILL PMT DOYLE", date(2026, 10, 14), -100000),
           T("T3", "BILL PMT DOYLE", date(2026, 10, 21), -82500)]
    assert sorted(match([inv], ts3).links[0].txn_ids) == ["T1", "T2", "T3"]


def test_split_not_forced_when_sum_is_off():
    inv = R("R9", "Doyle Studio", date(2026, 10, 6), 282500)
    ts = [T("T1", "INTERAC E-TRF SENT J DOYLE", date(2026, 10, 9), -150000),
          T("T2", "INTERAC E-TRF SENT J DOYLE", date(2026, 10, 23), -130000)]
    res = match([inv], ts)
    assert not res.links and len(res.unmatched_txns) == 2


def test_tip_mismatch_is_linked_and_flagged():
    res = match([R(merchant="Fennel & Ash Bistro", total=9492)], [T(desc="FENNEL & ASH BISTRO TORONT", amount=-11200)])
    (link,) = res.links
    assert link.kind == "mismatch" and link.amount_diff_cents == 1708 and "amount mismatch" in link.flags[0]


def test_mismatch_over_25_percent_is_not_linked():
    assert not match([R(total=1000)], [T(amount=-1300)]).links


def test_amount_only_link_requires_uniqueness():
    r = R(merchant="Lumen Lighting Studio", total=17854)
    one = match([r], [T(desc="2468013 ONTARIO INC", amount=-17854, day=date(2026, 10, 11))])
    assert one.links[0].kind == "amount_only" and "confirm" in one.links[0].flags[0]
    two = match([r], [T("T1", "2468013 ONTARIO INC", amount=-17854), T("T2", "1111111 ONTARIO INC", amount=-17854)])
    assert not two.links


def test_estimate_is_never_matched():
    res = match([R(is_receipt=False)], [T()])
    assert not res.links and res.not_receipts == ["R1"]


def test_duplicates():
    a = R("R1", invoice_number="BF-1")
    b = R("R2", invoice_number="BF-1")
    assert find_duplicate_receipts([b, a]) == {"R2": "R1"}
    assert find_duplicate_charges([T("T1"), T("T2"), T("T3", amount=-999)]) == [("T1", "T2")]
    res = match([a, b], [T()])
    assert len(res.links) == 1 and res.duplicate_receipts == {"R2": "R1"} and not res.orphan_receipts


# ---------------------------------------------------------------- flags and filing

@pytest.mark.parametrize("total,gst,bn,cur,flagged", [
    (9999, 1150, None, "CAD", False),      # under $100: number not required
    (10000, 1150, None, "CAD", True),      # $100.00 exactly: required
    (24800, 2418, None, "CAD", True),
    (24800, 2418, "123456789RT0001", "CAD", False),
    (11875, 0, None, "CAD", False),        # no GST/HST charged (exempt insurance)
    (4900, 0, None, "USD", False),
    (-24800, -2418, None, "CAD", True),    # refunds follow the same rule
])
def test_itc_registration_number_threshold(total, gst, bn, cur, flagged):
    r = R(total=total, cur=cur, gst_hst_cents=gst, gst_number=bn)
    assert bool(bk.receipt_flags(r)) is flagged


def test_evidence_path_format():
    assert str(bk.evidence_path(R(merchant="Fennel & Ash Bistro", total=24800), ".eml")) == \
        "evidence/2026-10/2026-10-10_fennel-ash-bistro_248.00.eml"
    assert str(bk.evidence_path(R(merchant="Cloudnest", total=4900, cur="USD"), ".pdf")).endswith(
        "_cloudnest_49.00USD.pdf")
    assert str(bk.evidence_path(R(merchant="Kestrel", total=-3276), ".eml")).endswith("_kestrel_-32.76.eml")
    assert bk.slug("") == "unknown"


# ---------------------------------------------------------------- rules extractor

@pytest.mark.parametrize("s,d", [("2026-10-06", date(2026, 10, 6)), ("Oct 2, 2026 at 9:14", date(2026, 10, 2)),
                                 ("October 19, 2026", date(2026, 10, 19)), ("21/10/2026", date(2026, 10, 21)),
                                 ("10/21/2026", date(2026, 10, 21)), ("nothing", None)])
def test_parse_date(s, d):
    assert bx.parse_date(s) == d


def _doc(body, subject="Your receipt from Test Shop", atts=()):
    return bx.ReceiptDoc("R1", Path("R1.eml"), "Test Shop <a@shop.test>", subject,
                         "Mon, 05 Oct 2026 10:00:00 -0400", body, list(atts))


def test_rules_reads_two_line_pdf_table_and_gst_number(chart):
    pdf = "Acme Studio\nINVOICE\nInvoice date: 2026-10-06\nDesign work\n$2,500.00\nSubtotal\n$2,500.00\n" \
          "HST (13%)\n$325.00\nTotal due (CAD)\n$2,825.00\nGST/HST Reg. No. 44438 8019 RT0001"
    f = bx.RulesExtractor(chart).read_receipt(_doc("see attached", atts=[("i.pdf", pdf)]))
    assert (f["merchant"], f["date"], f["total"], f["gst_hst"], f["gst_number"]) == \
        ("Acme Studio", "2026-10-06", 2825.0, 325.0, "444388019RT0001")


def test_rules_refund_is_negative_and_usd_detected(chart):
    x = bx.RulesExtractor(chart)
    f = x.read_receipt(_doc("Refund date: October 19, 2026\nHST refunded: CDN$ 3.77\nTotal refund: CDN$ 32.76",
                            subject="Refund issued for your Test order"))
    assert f["is_refund"] and f["total"] == -32.76 and f["gst_hst"] == -3.77
    g = x.read_receipt(_doc("Amount paid: US$49.00\nDate paid: October 3, 2026\nTax    US$0.00"))
    assert g["currency"] == "USD" and g["total"] == 49.0 and g["gst_hst"] == 0


def test_rules_does_not_read_subtotal_or_gst_number_as_amounts(chart):
    f = bx.RulesExtractor(chart).read_receipt(_doc("Subtotal   $20.00\nGST/HST #: 123456789 RT0001\nHST 13%   $2.60\n"
                                                    "Tip   $4.00\nTotal   $26.60"))
    assert f["total"] == 26.6 and f["gst_hst"] == 2.6 and f["tip"] == 4.0


def test_rules_flags_estimates(chart):
    f = bx.RulesExtractor(chart).read_receipt(_doc("x", atts=[("e.pdf", "Shop\nESTIMATE\nTotal due (CAD)\n$240.00")]))
    assert f["is_receipt"] is False


def test_rules_categorise(chart):
    x = bx.RulesExtractor(chart)
    t = lambda desc, amt: {"txn_id": "T", "date": "2026-10-10", "account": "VISA-4421", "description": desc,  # noqa: E731
                           "amount_cents": amt}
    assert x.categorise(t("STRIPE TRANSFER ST-1", 10000), None)["account_code"] == "4010"
    assert x.categorise(t("CIRCUIT HUB #118", -214587), {"summary": "Laptop 14in", "merchant": "Circuit Hub"})[
        "account_code"] == "1500"
    assert x.categorise(t("CIRCUIT HUB #118", -49900), {"summary": "Laptop 14in", "merchant": "Circuit Hub"})[
        "account_code"] != "1500"                                   # under the capitalisation threshold
    refund = x.categorise(t("KESTREL MKTP CA*RF8Q1 REFUND", 3276), {"summary": "Toner cartridge", "is_refund": True,
                                                                     "merchant": "Kestrel"})
    assert refund["account_code"] == "5040"                          # refund goes back to the expense
    owner = chart["entity"]["owner"].upper()
    assert x.categorise(t(f"INTERAC E-TRF SENT {owner}", -200000), None)["account_code"] == "3100"
    unknown = x.categorise(t("ZXQV 991", -1000), None)
    assert unknown["account_code"] == "9999" and unknown["confidence"] < bx.LOW_CONFIDENCE


def test_claude_extractor_rejects_codes_outside_chart(chart, monkeypatch):
    seen = {}

    def stub(**kw):                       # a stub of the API call, not a recorded response
        seen.update(kw)
        return {"account_code": "6666", "confidence": 0.99, "reason": "made up"}

    monkeypatch.setattr(bx.llm, "extract", stub)
    out = bx.ClaudeExtractor(chart).categorise(
        {"txn_id": "T", "date": "2026-10-10", "account": "VISA", "description": "X", "amount_cents": -100}, None)
    assert out["account_code"] == "9999" and out["confidence"] == 0.0
    assert seen["schema"] == bx.CATEGORY_SCHEMA and "5050  Software & Subscriptions" in seen["system"]


def test_claude_in_replay_without_fixtures_fails_clearly(chart, tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(llm.FixtureMissing):
        bx.ClaudeExtractor(chart, fixtures_dir=tmp_path).read_receipt(_doc("Total $1.00"))
    assert isinstance(bx.make_extractor(None, chart), bx.RulesExtractor)
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    assert isinstance(bx.make_extractor(None, chart), bx.ClaudeExtractor)


# ---------------------------------------------------------------- end to end

@pytest.fixture(scope="module")
def result(chart, tmp_path_factory):
    out = tmp_path_factory.mktemp("bk")
    return bk.run(bx.RulesExtractor(chart), bk.DATA, out, chart), out


def test_pipeline_finds_the_planted_cases(result):
    res, out = result
    kinds = {l.kind for l in res.matches.links}
    assert {"exact", "fx", "split", "mismatch", "amount_only"} <= kinds
    assert res.exceptions["duplicate_receipts"] and res.exceptions["duplicate_charges"]
    assert res.exceptions["missing_gst_number"] and res.exceptions["orphan_receipts"]
    assert all((out / p).exists() for p in res.evidence.values())
    for p in res.queue.items:                                     # every write is a proposal
        assert p.kind in {"evidence_link", "recategorise"} and p.decision == "pending"


def test_auto_approve_holds_flagged_and_updates_ledger(chart, tmp_path):
    res = bk.run(bx.RulesExtractor(chart), bk.DATA, tmp_path, chart)
    con = ledger.connect()
    ids = bk.load_ledger(con, res.txns)
    before = {r["id"]: (r["category"], r["evidence_path"]) for r in con.execute("select * from transactions")}
    res.queue.review(auto=True)
    done = bk.apply_approved(con, res.queue, ids, tmp_path)
    held = [p for p in res.queue.items if p.decision == "held"]
    assert held and all(p.flags for p in held)
    approved_links = [p for p in res.queue.items if p.kind == "evidence_link" and p.decision == "approved"]
    assert done["evidence_links"] == sum(len(p.payload["txn_ids"]) for p in approved_links)
    for p in held:                                                # held items leave the ledger untouched
        tids = p.payload.get("txn_ids") or [p.payload["txn_id"]]
        for tid in tids:
            row = con.execute("select category, evidence_path from transactions where id=?", (ids[tid],)).fetchone()
            if p.kind == "recategorise":
                assert row["category"] == before[ids[tid]][0]
    p = approved_links[0]
    row = con.execute("select evidence_path from transactions where id=?", (ids[p.payload["txn_ids"][0]],)).fetchone()
    assert row["evidence_path"].endswith(p.payload["evidence_path"])


def test_review_rejects_by_default(chart, tmp_path):
    res = bk.run(bx.RulesExtractor(chart), bk.DATA, tmp_path, chart)
    res.queue.review(ask=lambda _: "")
    assert all(p.decision == "rejected" for p in res.queue.items)


def test_run_cli_dry_run(tmp_path, capsys):
    import run as bk_run

    assert bk_run.main(["--out", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Dry run: ledger not changed" in text
    for f in ("proposals.json", "exceptions.md", "exceptions.csv", "ledger.db"):
        assert (tmp_path / f).exists()
    con = ledger.connect(tmp_path / "ledger.db")
    assert con.execute("select count(*) from transactions where evidence_path is not null").fetchone()[0] == 0


# ---------------------------------------------------------------- generator

def _hashes(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_generator_is_reproducible_and_matches_committed_data(tmp_path):
    a = tmp_path / "a"
    stats = bookkeeping_data.main(a)
    assert stats["transactions"] >= 80 and stats["links"] >= 15
    assert _hashes(a) == _hashes(bk.DATA)


def test_labels_are_consistent():
    labels = json.loads((bk.DATA / "labels.json").read_text())
    txn_ids = {t["txn_id"] for t in bk.load_transactions()}
    assert {tid for _, tid in labels["links"]} <= txn_ids
    assert set(labels["categories"]) == txn_ids
    assert sum(1 for g in labels["receipts"].values() if g["orphan"]) >= 3
