import json

import pytest

from shared import approvals, ledger, llm, mail, tax


def test_hst_rounding():
    assert tax.gst_hst_cents(10000, "ON") == 1300
    assert tax.gst_hst_cents(999, "AB") == 50


def test_ledger_roundtrip():
    con = ledger.connect()
    cid = ledger.upsert_customer(con, "Acme", "a@x.test", "ON")
    assert ledger.upsert_customer(con, "Acme", "a@x.test", "ON") == cid
    iid = ledger.create_invoice(con, number="INV-1", customer_id=cid, period="2026-10",
                                issue_date="2026-10-25", due_date="2026-11-24",
                                lines=[{"desc": "x", "amount_cents": 10000}], tax_code="HST", tax_cents=1300)
    assert con.execute("select subtotal_cents from invoices where id=?", (iid,)).fetchone()[0] == 10000


def test_auto_review_holds_flagged():
    q = approvals.ApprovalQueue()
    q.propose("bill", "ok", {})
    q.propose("bill", "odd", {}, flags=["amount mismatch"])
    approved = q.review(auto=True)
    assert len(approved) == 1 and q.items[1].decision == "held"


def test_replay_missing_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(llm.FixtureMissing):
        llm.extract(module="t", task="t", system="s", prompt="p", schema={"type": "object"}, fixtures_dir=tmp_path)


def test_replay_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    key = llm._key("s", "p", {"type": "object"})
    (tmp_path / f"t-{key}.json").write_text(json.dumps({"output": {"a": 1}}))
    assert llm.extract(module="t", task="t", system="s", prompt="p", schema={"type": "object"}, fixtures_dir=tmp_path) == {"a": 1}


def test_eml_roundtrip(tmp_path):
    (tmp_path / "1.eml").write_bytes(mail.build_eml(sender="v@x.test", to="ap@y.test", subject="Inv 7",
                                                   date="Mon, 02 Nov 2026 09:00:00 -0500", body="hi",
                                                   attachments=[("i.txt", "text/plain", b"total 113.00")]))
    m = mail.read_mailbox(tmp_path)[0]
    assert m.subject == "Inv 7" and m.attachments[0].data == b"total 113.00"


def test_update_customer_and_transaction_whitelist():
    con = ledger.connect()
    cid = ledger.upsert_customer(con, "Acme", "a@x.test", "ON")
    assert ledger.update_customer(con, cid, email="b@x.test", province="BC") == 1
    assert tuple(con.execute("select email, province from customers").fetchone()) == ("b@x.test", "BC")
    with pytest.raises(ValueError):
        ledger.update_customer(con, cid, id=99)
    tid = ledger.add_transaction(con, date="2026-10-01", description="x", amount_cents=-100, account="bank")
    assert ledger.update_transaction(con, tid, category="office", evidence_path="e/1.pdf") == 1
    assert ledger.update_transaction(con, tid) == 0
    with pytest.raises(ValueError):
        ledger.update_transaction(con, tid, **{"category = 'x'; --": 1})


def test_migration_upgrades_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("create table bills (id integer primary key, vendor_id integer, number text);"
                      "create table invoices (id integer primary key, number text unique);"
                      "insert into bills (vendor_id, number) values (1, 'A-1');")
    old.commit()
    old.close()
    con = ledger.connect(db)
    assert {"currency", "total_cents", "other_tax_cents"} <= ledger.columns(con, "bills")
    assert {"po_number", "currency", "provincial_tax_cents"} <= ledger.columns(con, "invoices")
    assert {"other_tax_kind", "qst_itr_cents"} <= ledger.columns(con, "bills")
    assert tuple(con.execute("select number, currency, other_tax_cents, qst_itr_cents from bills").fetchone()) \
        == ("A-1", "CAD", 0, 0)
    assert ledger.migrate(con) == []          # idempotent


def test_invoice_po_and_currency():
    con = ledger.connect()
    iid = ledger.create_invoice(con, number="INV-2", customer_id=1, period="2026-10", issue_date="2026-10-25",
                                due_date="2026-11-24", lines=[{"amount_cents": 100}], tax_code="HST", tax_cents=13,
                                po_number="PO-7")
    assert tuple(con.execute("select po_number, currency from invoices where id=?", (iid,)).fetchone()) == ("PO-7", "CAD")


def test_eml_pinned_boundary():
    kw = dict(sender="v@x.test", to="ap@y.test", subject="s", date="Mon, 02 Nov 2026 09:00:00 -0500", body="hi",
              attachments=[("i.txt", "text/plain", b"x")])
    a, b = mail.build_eml(**kw, boundary="==fixed=="), mail.build_eml(**kw, boundary="==fixed==")
    assert a == b and b'boundary="==fixed=="' in a
    assert mail.build_eml(**{**kw, "attachments": ()}, boundary="==fixed==").count(b"boundary") == 0


def test_provincial_tax_rates_and_rounding():
    # 9.975% of $100.00 is 997.5 cents: half-up gives 998. Float maths would risk 997.
    assert tax.qst_cents(10000) == 998
    assert tax.qst_cents(50000) == 4988       # $500.00 -> $49.88, as on a Quebec invoice
    assert tax.qst_cents(200) == 20           # 19.95 cents rounds up
    assert tax.bc_pst_cents(18000) == 1260
    assert tax.sk_pst_cents(10050) == 603
    assert tax.mb_rst_cents(76000) == 5320
    assert tax.provincial_tax_cents(10000, "ON") == 0
    assert tax.provincial_tax_cents(10000, "AB") == 0
    assert [tax.provincial_tax_code(p) for p in ("QC", "BC", "SK", "MB", "ON", None)] == \
        ["QST", "PST", "PST", "RST", None, None]
    assert tax.provincial_tax_label("QC") == "QST 9.975%"


def test_provincial_rates_agree_with_rates_table():
    from decimal import Decimal
    for prov, (_, rate, _) in tax.PROVINCIAL_TAX.items():
        assert Decimal(rate) == Decimal(str(tax.RATES[prov][1]))


@pytest.mark.parametrize("raw,ok,reason", [
    ("1006543210TQ0001", True, "ok"),
    ("1006543210 tq 0001", True, "ok"),
    ("1006543210-TQ-0002", True, "ok"),
    ("100654321TQ0001", False, "not 10 digits + TQ + 4 digits"),
    ("1006543210TQ001", False, "not 10 digits + TQ + 4 digits"),
    ("1006543210RT0001", False, "not 10 digits + TQ + 4 digits"),
    ("NR12345678", False, "NR number (specified QST system); confirm with Revenu Quebec"),
    ("", False, "missing"),
])
def test_qst_number(raw, ok, reason):
    assert tax.validate_qst_number(raw) == (ok, reason)
    assert tax.qst_number_format_ok(raw) is ok


def test_provincial_tax_columns():
    con = ledger.connect()
    assert {"provincial_tax_cents", "provincial_tax_code"} <= ledger.columns(con, "invoices")
    assert {"other_tax_kind", "qst_itr_cents"} <= ledger.columns(con, "bills")
    iid = ledger.create_invoice(con, number="INV-3", customer_id=1, period="2026-10", issue_date="2026-10-25",
                                due_date="2026-11-24", lines=[{"amount_cents": 10000}], tax_code="GST-QC",
                                tax_cents=500, provincial_tax_cents=998, provincial_tax_code="QST")
    row = con.execute("select tax_cents, provincial_tax_cents, provincial_tax_code from invoices where id=?",
                      (iid,)).fetchone()
    assert tuple(row) == (500, 998, "QST")
    vid = ledger.upsert_vendor(con, "Q Inc.", qst_number="1006543210TQ0001")
    assert con.execute("select qst_number from vendors where id=?", (vid,)).fetchone()[0] == "1006543210TQ0001"


@pytest.mark.parametrize("raw,ok,reason", [
    ("123456782RT0001", True, "ok"),
    ("123456782 rt 0001", True, "ok"),
    ("123456781RT0001", False, "fails the BN check digit"),
    ("12345678RT0001", False, "not 9 digits + RT + 4 digits"),
    ("123456782RC0001", False, "not 9 digits + RT + 4 digits"),
    (None, False, "missing"),
])
def test_gst_hst_number(raw, ok, reason):
    assert tax.validate_gst_hst_number(raw) == (ok, reason)
    assert tax.gst_hst_number_format_ok(raw) == (reason not in ("missing", "not 9 digits + RT + 4 digits"))
    assert tax.bn_check_digit("12345678") == 2
