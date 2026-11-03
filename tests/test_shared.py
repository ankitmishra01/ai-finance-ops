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
    assert {"po_number", "currency"} <= ledger.columns(con, "invoices")
    assert tuple(con.execute("select number, currency, other_tax_cents from bills").fetchone()) == ("A-1", "CAD", 0)
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
