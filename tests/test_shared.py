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
