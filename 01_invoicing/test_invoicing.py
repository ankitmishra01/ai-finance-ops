import json
from datetime import date

import pytest

from shared import ledger, llm, tax
from shared.approvals import ApprovalQueue

import invoicing
import invoicing_data
from invoicing import (DraftInvoice, Line, check_cra_fields, cra_tier, due_date, line_amount_cents,
                       plan_month, tax_for)
from invoicing_extract import (SCHEMA, ClaudeExtractor, Email, ExtractionInput, RulesExtractor,
                               get_extractor, normalize)
from invoicing_pdf import render_invoice

PERIOD = "2026-10"
ISSUE = date(2026, 10, 25)


@pytest.fixture(scope="module")
def ops():
    return invoicing.load_ops()


@pytest.fixture(scope="module")
def plan(ops):
    return plan_month(ops, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())


def invs(plan, cid):
    return [i for i in plan.invoices if i.client_id == cid]


# ---- maths -----------------------------------------------------------------

@pytest.mark.parametrize("hours,rate,expected", [
    (1.25, 17500, 21875),
    (0.1, 12345, 1235),       # 1234.5 -> half-up
    ("7.5", 18500, 138750),
    (0.5, 15000, 7500),
    (0, 20000, 0),
])
def test_line_amount_half_up(hours, rate, expected):
    assert line_amount_cents(hours, rate) == expected


@pytest.mark.parametrize("prov,sub,tax_c,label", [
    ("ON", 194250, 25253, "HST 13%"),   # 25252.5 rounds up
    ("NS", 300000, 42000, "HST 14%"),
    ("NB", 120000, 18000, "HST 15%"),
    ("AB", 7500, 375, "GST 5%"),
    ("QC", 1800000, 90000, "GST 5%"),   # GST only; QST is provincial_tax_for
    ("BC", 1, 0, "GST 5%"),
    ("SK", 10, 1, "GST 5%"),            # 0.5 cent rounds up
])
def test_tax_by_place_of_supply(prov, sub, tax_c, label):
    t, code, lab = tax_for(sub, prov)
    assert (t, lab) == (tax_c, label) and code.endswith(prov)


def test_tax_unknown_province():
    with pytest.raises(ValueError):
        tax_for(100, "XX")


@pytest.mark.parametrize("issue,terms,expected", [
    (date(2026, 10, 25), "Net 30", date(2026, 11, 24)),
    (date(2026, 10, 25), "net 15", date(2026, 11, 9)),
    (date(2026, 10, 25), "Net 45", date(2026, 12, 9)),
    (date(2026, 10, 25), "Due on receipt", date(2026, 10, 25)),
    (date(2026, 12, 25), "Net 30", date(2027, 1, 24)),
    (date(2028, 1, 15), "Net 30 EOM", date(2028, 3, 1)),   # leap year
    (date(2026, 10, 25), "2/10 Net 30", date(2026, 11, 24)),
])
def test_due_date(issue, terms, expected):
    assert due_date(issue, terms) == expected


def test_due_date_rejects_unknown_terms():
    with pytest.raises(ValueError):
        due_date(ISSUE, "whenever")


@pytest.mark.parametrize("total,tier", [
    (9999, "under $100"), (10000, "$100 to $499.99"), (49999, "$100 to $499.99"), (50000, "$500 or more"),
])
def test_cra_tier_boundaries(total, tier):
    assert cra_tier(total) == tier


def _inv(total, **kw):
    base = dict(number="INV-1", client_id="X", contract_ids=["K"], period=PERIOD, issue_date="2026-10-25",
                due_date="2026-11-24", payment_terms="Net 30", customer={"legal_name": "X Inc."},
                place_of_supply="ON", lines=[Line("Advice", "1", total, total, "retainer", "K")],
                subtotal_cents=total, tax_cents=0, tax_code="HST-ON", tax_label="HST 13%", total_cents=total,
                po_number=None, cra_tier=cra_tier(total))
    base.update(kw)
    return DraftInvoice(**base)


def test_cra_fields_registration_number_needed_from_100():
    sup = {"name": "S Inc.", "gst_hst_number": ""}
    assert check_cra_fields(_inv(9999), sup) == []
    assert "supplier_gst_hst_number" in check_cra_fields(_inv(10000), sup)
    assert check_cra_fields(_inv(10000), {"name": "S Inc.", "gst_hst_number": "123456789 RT0001"}) == []


def test_cra_fields_customer_name_and_terms_needed_from_500():
    sup = {"name": "S Inc.", "gst_hst_number": "123456789 RT0001"}
    inv = _inv(50000, customer={"legal_name": None}, payment_terms="")
    assert set(check_cra_fields(inv, sup)) == {"customer_name", "payment_terms"}
    assert check_cra_fields(_inv(49999, customer={"legal_name": None}, payment_terms=""), sup) == []


REGS = {"QC": {"registered": True, "number": "1218654370TQ0001"}, "BC": {"registered": True, "number": "PST-1"},
        "SK": {"registered": False}}


@pytest.mark.parametrize("prov,sub,expected", [
    ("QC", 1800000, (179550, "QST", "QST 9.975%", "1218654370TQ0001")),
    ("QC", 10000, (998, "QST", "QST 9.975%", "1218654370TQ0001")),   # 997.5 rounds up
    ("BC", 192500, (13475, "PST", "PST 7%", "PST-1")),
    ("SK", 112500, (0, "", "", "")),      # not registered in SK
    ("MB", 100000, (0, "", "", "")),      # not in the registrations at all
    ("ON", 100000, (0, "", "", "")),      # HST province: no separate provincial tax
    (None, 100000, (0, "", "", "")),
])
def test_provincial_tax_only_where_registered(prov, sub, expected):
    assert invoicing.provincial_tax_for(sub, prov, REGS) == expected


def test_supplier_config_loaded_with_ops(ops):
    regs = ops["supplier"]["provincial_registrations"]
    assert regs["QC"]["registered"] and regs["BC"]["registered"]
    assert not regs["SK"]["registered"] and not regs["MB"]["registered"]
    assert "provincial_registrations" not in invoicing.load_ops(supplier_config=None)["supplier"]


# ---- month plan on the synthetic data --------------------------------------

def test_invoice_arithmetic_and_numbering(plan):
    nums = [i.number for i in plan.invoices]
    assert len(nums) == len(set(nums))
    assert nums == sorted(nums)
    for i in plan.invoices:
        assert i.subtotal_cents == sum(ln.amount_cents for ln in i.lines)
        assert i.total_cents == i.subtotal_cents + i.tax_cents + i.provincial_tax_cents
        assert i.tax_cents == tax_for(i.subtotal_cents, i.place_of_supply)[0]


def test_not_billed(plan):
    reasons = {n["client_id"]: n["reason"] for n in plan.not_billed}
    assert reasons == {"C013": "no active contract in period", "C014": "no billable activity in period"}


@pytest.mark.parametrize("cid,needle", [
    ("C003", "active retainer but no logged activity"),
    ("C004", "differs from contract rate"),
    ("C006", "missing billing email"),
    ("C007", "duplicate: INV-01038"),
    ("C008", "PO required"),
    ("C016", "exceeds the contract cap"),
])
def test_flags(plan, cid, needle):
    (inv,) = invs(plan, cid)
    assert any(needle in f for f in inv.flags), inv.flags


def test_clean_invoice_has_no_flags(plan):
    assert invs(plan, "C001")[0].flags == []


def test_off_rate_entry_billed_at_contract_rate(plan, ops):
    (inv,) = invs(plan, "C004")
    hours = sum(t["hours"] for t in ops["timesheets"] if t["contract_id"] == "K004")
    assert inv.subtotal_cents == line_amount_cents(hours, 17500)


def test_split_request_gives_two_invoices(plan):
    two = invs(plan, "C009")
    assert [i.contract_ids for i in two] == [["K009A"], ["K009B"]]


def test_address_change_moves_place_of_supply(plan):
    (inv,) = invs(plan, "C012")
    assert inv.place_of_supply == "SK" and inv.tax_label == "GST 5%"


def test_new_po_from_email_clears_po_flag(plan):
    (inv,) = invs(plan, "C011")
    # Rules baseline keeps the old PO (a known miss, see eval); it still clears the PO-required flag.
    assert inv.po_number and not any("PO required" in f for f in inv.flags)


def test_milestone_only_completed_in_period(plan):
    (inv,) = invs(plan, "C005")
    assert [ln.amount_cents for ln in inv.lines] == [1800000]
    assert inv.due_date == inv.issue_date  # due on receipt


def test_qst_and_pst_on_the_month(plan):
    (qc,) = invs(plan, "C005")
    assert (qc.place_of_supply, qc.tax_cents, qc.provincial_tax_code, qc.provincial_tax_cents) == \
        ("QC", 90000, "QST", 179550)
    assert qc.total_cents == 1800000 + 90000 + 179550
    assert not qc.flags                       # QST needs no extra review
    (bc,) = invs(plan, "C004")
    assert bc.provincial_tax_code == "PST" and bc.provincial_tax_cents == tax.bc_pst_cents(bc.subtotal_cents)
    assert any("BC PST applies only to listed services" in f for f in bc.flags)
    # SK (C007, and C012 after its move) and MB (C010): supplier not registered, GST only.
    for cid in ("C007", "C010", "C012"):
        assert all(i.provincial_tax_cents == 0 and i.provincial_tax_code == "" for i in invs(plan, cid))
    # HST provinces never get a separate provincial tax.
    assert all(i.provincial_tax_cents == 0 for i in plan.invoices if i.place_of_supply in ("ON", "NS", "NB", "PE"))


def test_no_registrations_means_gst_hst_only():
    ops = invoicing.load_ops(supplier_config=None)
    plan = plan_month(ops, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    assert all(i.provincial_tax_cents == 0 for i in plan.invoices)


def test_registering_in_sk_charges_pst_and_flags_taxability(ops):
    ops2 = json.loads(json.dumps(ops))
    ops2["supplier"]["provincial_registrations"]["SK"] = {"registered": True, "number": ""}
    plan = plan_month(ops2, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    (inv,) = invs(plan, "C007")
    assert inv.provincial_tax_cents == tax.sk_pst_cents(inv.subtotal_cents) > 0
    assert any("no SK registration number" in f for f in inv.flags)


def test_qst_without_valid_number_is_flagged(ops):
    ops2 = json.loads(json.dumps(ops))
    ops2["supplier"]["provincial_registrations"]["QC"]["number"] = "12345TQ1"
    plan = plan_month(ops2, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    (inv,) = invs(plan, "C005")
    assert any("QST number is not 10 digits + TQ + 4 digits" in f for f in inv.flags)


def test_small_invoice_tier(plan):
    (inv,) = invs(plan, "C015")
    assert inv.total_cents == 7875 and inv.cra_tier == "under $100"


def test_new_clients_become_customer_proposals(plan):
    new = {c.client_id: c for c in plan.customers if c.is_new}
    assert set(new) == {"N001", "N002", "N003", "N004"}
    assert new["N004"].billing_email.startswith("payables@")   # later correction wins
    assert "missing billing email" in new["N003"].flags


# ---- approvals, ledger and PDFs ---------------------------------------------

def test_auto_approve_holds_flagged_and_blocks_on_rejected_customer(ops):
    plan = plan_month(ops, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    q, deps = invoicing.build_queue(plan)
    q.review(auto=True)
    invoicing.enforce_dependencies(q, deps)
    for p in q.items:
        assert p.decision == ("held" if p.flags else "approved")
    # Reject the C010 billing-email change: its invoice must not go out.
    q2, deps2 = invoicing.build_queue(plan)
    answers = iter(["n" if (p.kind == "customer_update" and p.payload["client_id"] == "C010") else "y"
                    for p in q2.items])
    q2.review(ask=lambda _: next(answers))
    invoicing.enforce_dependencies(q2, deps2)
    c010 = next(p for p in q2.items if p.kind == "invoice" and p.payload["client_id"] == "C010")
    assert c010.decision == "held"


def test_commit_writes_ledger_pdfs_and_detects_rerun_duplicates(ops, tmp_path):
    plan = plan_month(ops, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    q, deps = invoicing.build_queue(plan)
    q.review(auto=True)
    invoicing.enforce_dependencies(q, deps)
    con = ledger.connect(tmp_path / "l.db")
    written = invoicing.commit(q, plan, con, tmp_path)
    approved = [p for p in q.items if p.kind == "invoice" and p.decision == "approved"]
    assert written["invoices"] == len(approved) > 0
    rows = con.execute("select number, status, subtotal_cents, tax_cents from invoices").fetchall()
    assert {r["status"] for r in rows} == {"approved"}
    assert {r["number"] for r in rows} == {p.payload["number"] for p in approved}
    # C010's approved email change is in the ledger
    assert con.execute("select email from customers where email like 'finance.ops@%'").fetchone()

    pdf = (tmp_path / "invoices" / f"{approved[0].payload['number']}.pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    for needle in [ops["supplier"]["gst_hst_number"], approved[0].payload["customer"]["legal_name"],
                   "Payment terms", "Total \\(CAD\\)"]:
        assert needle.encode() in pdf, needle

    top = max(int(p.payload["number"].split("-")[1]) for p in approved)
    assert invoicing.next_seq_from_ledger(con) == top + 1

    # The Quebec invoice is clean, so it is approved: QST sits in its own ledger column and on the PDF.
    qc = next(p.payload for p in approved if p.payload["client_id"] == "C005")
    row = con.execute("select tax_cents, provincial_tax_cents, provincial_tax_code from invoices where number=?",
                      (qc["number"],)).fetchone()
    assert tuple(row) == (90000, 179550, "QST")
    qc_pdf = (tmp_path / "invoices" / f"{qc['number']}.pdf").read_bytes()
    for needle in ["QST 9.975% \\(QC\\)", "$1,795.50", "QST registration no.: 1218654370TQ0001", "$20,695.50"]:
        assert needle.encode() in qc_pdf, needle
    on = next(p.payload for p in approved if p.payload["place_of_supply"] == "ON")
    on_pdf = (tmp_path / "invoices" / f"{on['number']}.pdf").read_bytes()
    assert b"QST" not in on_pdf and b"PST" not in on_pdf

    again = plan_month(ops, invoicing.load_mail(), PERIOD, ISSUE, RulesExtractor())
    invoicing.flag_ledger_duplicates(again, con)
    c001 = invs(again, "C001")[0]
    assert any("ledger already has" in f for f in c001.flags)


def test_draft_pdf_marks_draft(tmp_path, plan):
    p = render_invoice(plan.invoices[0].to_dict(), plan.supplier, tmp_path / "d.pdf", draft=True)
    assert b"DRAFT - NOT ISSUED" in p.read_bytes()


# ---- extractor -------------------------------------------------------------

def _email(body, sender='"Jo Lee" <jo.lee@acme.test>', date="Mon, 05 Oct 2026 09:00:00 -0400"):
    return Email(sender, "s", date, body)


def test_rules_extractor_contract_and_email():
    contract = ('This engagement letter is made between Sup Inc. ("Consultant") and Acme Widgets Ltd. '
                '("Client"), having its office at 12 King St W, Toronto, ON M5H 1A1.')
    got = RulesExtractor().extract(ExtractionInput("Acme", "Sup Inc.", contract, [
        _email("Please send invoices to ap@acme.test. Our PO number for this work is 4500012345.")]))
    assert got["legal_name"] == "Acme Widgets Ltd."
    assert got["billing_email"] == "ap@acme.test"
    assert got["province"] == "ON" and got["address"] == "12 King St W, Toronto, ON M5H 1A1"
    assert got["po_number"] == "4500012345" and got["split_invoices"] is False


def test_rules_extractor_ignores_quoted_history_and_no_po():
    body = ("Invoices should now go to new@acme.test.\n\n> Please send invoices to old@acme.test\n"
            "We don't use purchase orders.")
    got = RulesExtractor().extract(ExtractionInput("Acme", "Sup", None, [_email(body)]))
    assert got["billing_email"] == "new@acme.test" and got["po_number"] is None


def test_rules_invoice_me_uses_sender():
    got = RulesExtractor().extract(ExtractionInput("Acme", "Sup", None, [_email("You can invoice me directly.")]))
    assert got["billing_email"] == "jo.lee@acme.test"


def test_normalize():
    assert normalize("po_number", "P.O. #551989") == "551989"
    assert normalize("po_number", "PO-2026-1234") == normalize("po_number", "2026-1234")
    assert normalize("address", "1 Main St, Halifax, Nova Scotia B3H1A1") == normalize("address", "1 Main St, Halifax, NS B3H 1A1")
    assert normalize("billing_email", "AP@X.test") == "ap@x.test"
    assert normalize("legal_name", None) is None


def test_schema_requires_every_property():
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    assert None in SCHEMA["properties"]["province"]["enum"]


def test_get_extractor_by_mode(monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    assert get_extractor().name == "rules"
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    assert get_extractor().name == "claude"
    assert get_extractor("rules").name == "rules"


def test_claude_extractor_in_replay_without_recording_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(llm.FixtureMissing):
        ClaudeExtractor(tmp_path).extract(ExtractionInput("Acme", "Sup", None, [_email("hi")]))


# ---- data -------------------------------------------------------------------

def test_generator_is_deterministic(tmp_path):
    invoicing_data.generate(tmp_path)
    committed = invoicing.DATA
    assert (tmp_path / "ops.json").read_text() == (committed / "ops.json").read_text()
    assert json.loads((tmp_path / "eval" / "labels.json").read_text()) == \
        json.loads((committed / "eval" / "labels.json").read_text())
    assert len(json.loads((committed / "eval" / "labels.json").read_text())) >= 15
