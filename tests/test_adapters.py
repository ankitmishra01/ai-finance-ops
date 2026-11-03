"""Adapter tests. All HTTP and IMAP traffic is faked; responses come from tests/schema_mocks.py,
which are hand-built from the platforms' documented schemas, not recorded from real accounts."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import schema_mocks as M

from shared import ledger
from shared.adapters import AdapterConfigError, ApiError, get_ledger, get_mailbox
from shared.adapters.http import OAuth2, Response
from shared.adapters.imap import ImapMailbox
from shared.adapters.quickbooks import QuickBooksLedger
from shared.adapters.xero import XeroLedger
from shared.mail import EmlMailbox, build_eml
from shared.protocols import LedgerAdapter, Mailbox, check_balanced


class FakeTransport:
    """Serves queued (method, url fragment, status, body, headers) responses in order and records calls."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body and headers.get("Content-Type") == "application/json"
                           else body})
        assert self.routes, f"unexpected request {method} {url}"
        m, frag, status, resp, hdrs = (*self.routes.pop(0), {})[:5]
        assert method == m and frag in url, f"expected {m} ...{frag}..., got {method} {url}"
        return Response(status, hdrs or {}, resp)

    def api_calls(self):
        return [c for c in self.calls if "oauth" not in c["url"] and "identity.xero.com" not in c["url"]]


def qbo(*routes, **kw):
    t = FakeTransport(("POST", "oauth2/v1/tokens/bearer", 200, M.QBO_TOKEN), *routes)
    auth = OAuth2("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", "cid", "csecret", "mock-refresh-0",
                  transport=t)
    sleeps = []
    led = QuickBooksLedger(realm_id="9130000000000", auth=auth, transport=t, sleep=sleeps.append,
                           item_id="1", expense_account_id="7", **kw)
    return led, t, sleeps


def xero(*routes, **kw):
    t = FakeTransport(("POST", "identity.xero.com/connect/token", 200, M.XERO_TOKEN), *routes)
    auth = OAuth2("https://identity.xero.com/connect/token", "cid", "csecret", "mock-refresh-0", transport=t)
    led = XeroLedger(tenant_id="tenant-123", auth=auth, transport=t, sleep=lambda s: None,
                     sales_account_code="200", expense_account_code="400", **kw)
    return led, t


def params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# --- protocols and defaults ------------------------------------------------------------------------

def test_every_ledger_and_mailbox_implements_the_protocols(tmp_path):
    assert isinstance(ledger.SQLiteLedger(), LedgerAdapter)
    assert isinstance(qbo()[0], LedgerAdapter)
    assert isinstance(xero()[0], LedgerAdapter)
    assert isinstance(EmlMailbox(tmp_path), Mailbox)
    assert isinstance(ImapMailbox(user="u", password="p"), Mailbox)


def test_defaults_are_sqlite_and_eml(tmp_path, monkeypatch):
    monkeypatch.delenv("AFO_LEDGER", raising=False)
    monkeypatch.delenv("AFO_MAILBOX", raising=False)
    assert get_ledger().name == "sqlite"
    (tmp_path / "1.eml").write_bytes(build_eml(sender="a@x.test", to="b@y.test", subject="s",
                                               date="Mon, 02 Nov 2026 09:00:00 -0500", body="hi"))
    mb = get_mailbox(tmp_path)
    assert mb.name == "eml" and [m.subject for m in mb.fetch()] == ["s"]


@pytest.mark.parametrize("var,value,missing", [
    ("AFO_LEDGER", "quickbooks", "QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REFRESH_TOKEN, QBO_REALM_ID"),
    ("AFO_LEDGER", "xero", "XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REFRESH_TOKEN, XERO_TENANT_ID"),
    ("AFO_MAILBOX", "imap", "AFO_IMAP_USER"),
])
def test_opt_in_adapters_need_their_env_and_never_echo_values(monkeypatch, var, value, missing, tmp_path):
    for n in missing.split(", "):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv(var, value)
    monkeypatch.setenv("QBO_CLIENT_SECRET" if value == "quickbooks" else "XERO_CLIENT_SECRET", "s3cret-value")
    with pytest.raises(AdapterConfigError) as e:
        get_ledger() if var == "AFO_LEDGER" else get_mailbox(tmp_path)
    assert "s3cret-value" not in str(e.value)
    for n in missing.split(", "):
        if n not in ("QBO_CLIENT_SECRET", "XERO_CLIENT_SECRET"):
            assert n in str(e.value)


def test_unknown_adapter_name(monkeypatch, tmp_path):
    monkeypatch.setenv("AFO_LEDGER", "wave")
    with pytest.raises(AdapterConfigError):
        get_ledger()


def test_check_balanced():
    assert check_balanced([{"account": "a", "debit_cents": 500}, {"account": "b", "credit_cents": 500}]) == (500, 500)
    for bad in ([{"account": "a", "debit_cents": 5}],
                [{"account": "a", "debit_cents": 5}, {"account": "b", "credit_cents": 4}],
                [{"account": "a", "debit_cents": 5, "credit_cents": 5}, {"account": "b", "credit_cents": 0}],
                [{"account": "", "debit_cents": 5}, {"account": "b", "credit_cents": 5}]):
        with pytest.raises(ValueError):
            check_balanced(bad)


def test_sqlite_ledger_journal_and_bills_post_through_adapter():
    led = ledger.SQLiteLedger()
    jid = led.post_journal_entry(date="2026-11-13", memo="pay run", source="payroll",
                                 lines=[{"account": "wages", "debit_cents": 1000},
                                        {"account": "bank", "credit_cents": 1000}])
    assert led.con.execute("select debit_cents, credit_cents from journal_entries where id=?",
                           (jid,)).fetchone()[:] == (1000, 1000)
    vid = led.upsert_vendor("V")
    bid = led.create_bill(vendor_id=vid, number="B-1", subtotal_cents=100, tax_cents=13, status="approved")
    led.commit()
    assert ledger.as_adapter(led.con).con is led.con and ledger.as_adapter(led) is led
    assert led.con.execute("select number from bills where id=?", (bid,)).fetchone()[0] == "B-1"


# --- QuickBooks Online -----------------------------------------------------------------------------

def test_qbo_creates_customer_after_empty_query():
    led, t, _ = qbo(("GET", "/query", 200, M.QBO_QUERY_EMPTY), ("POST", "/customer?", 200, M.QBO_CUSTOMER))
    assert led.upsert_customer("Acme Synthetic Ltd.", "ap@acme.example", "ON", "1 Example St") == "58"
    q, create = t.api_calls()
    assert q["url"].startswith("https://sandbox-quickbooks.api.intuit.com/v3/company/9130000000000/query?")
    assert params(q["url"])["query"] == "select * from Customer where DisplayName = 'Acme Synthetic Ltd.'"
    assert params(create["url"])["minorversion"] == "75" and len(params(create["url"])["requestid"]) <= 50
    assert create["headers"]["Authorization"] == "Bearer mock-access-1"
    assert create["body"]["BillAddr"] == {"Line1": "1 Example St", "CountrySubDivisionCode": "ON", "Country": "CA"}
    assert create["body"]["PrimaryEmailAddr"] == {"Address": "ap@acme.example"}


def test_qbo_existing_customer_is_not_recreated_and_quotes_are_escaped():
    led, t, _ = qbo(("GET", "/query", 200, M.qbo_query("Customer", [M.QBO_CUSTOMER["Customer"]])))
    assert led.upsert_customer("O'Brien & Co", "", "ON") == "58"
    assert params(t.api_calls()[0]["url"])["query"].endswith("DisplayName = 'O\\'Brien & Co'")


def test_qbo_update_customer_uses_synctoken_and_sparse_update():
    led, t, _ = qbo(("GET", "/customer/58", 200, M.QBO_CUSTOMER), ("POST", "/customer?", 200, M.QBO_CUSTOMER))
    assert led.update_customer("58", email="new@acme.example", province="BC") == 1
    body = t.api_calls()[1]["body"]
    assert body["Id"] == "58" and body["SyncToken"] == "0" and body["sparse"] is True
    assert body["BillAddr"]["CountrySubDivisionCode"] == "BC" and "Id" not in body["BillAddr"]
    with pytest.raises(ValueError):
        led.update_customer("58", Balance=0)


def test_qbo_invoice_maps_lines_and_tax_codes_and_checks_tax():
    led, t, _ = qbo(("POST", "/invoice?", 200, M.QBO_INVOICE), tax_code_map={"HST ON": "13"})
    iid = led.create_invoice(number="INV-1001", customer_id="58", period="2026-10", issue_date="2026-10-31",
                             due_date="2026-11-30", tax_code="HST ON", tax_cents=13000, po_number="PO-7",
                             lines=[{"desc": "Consulting", "qty": 10, "unit_cents": 10000, "amount_cents": 100000}])
    assert iid == "130" and led.warnings == []
    body = t.api_calls()[0]["body"]
    line = body["Line"][0]
    assert line["DetailType"] == "SalesItemLineDetail" and line["Amount"] == 1000.0
    assert line["SalesItemLineDetail"] == {"ItemRef": {"value": "1"}, "Qty": 10, "UnitPrice": 100.0,
                                           "TaxCodeRef": {"value": "13"}}
    assert body["CustomerRef"] == {"value": "58"} and body["DocNumber"] == "INV-1001"
    assert body["GlobalTaxCalculation"] == "TaxExcluded" and "PO PO-7" in body["PrivateNote"]

    led2, _, _ = qbo(("POST", "/invoice?", 200, M.QBO_INVOICE))
    led2.create_invoice(number="INV-1001", customer_id="58", period="2026-10", issue_date="2026-10-31",
                        due_date="2026-11-30", tax_code="HST ON", tax_cents=12900,
                        lines=[{"desc": "x", "amount_cents": 100000}])
    assert "QuickBooks tax 130.0 != agent tax 129.00" in led2.warnings[0]


def test_qbo_vendor_bill_and_journal():
    led, t, _ = qbo(("GET", "/query", 200, M.QBO_QUERY_EMPTY), ("POST", "/vendor?", 200, M.QBO_VENDOR),
                    ("POST", "/bill?", 200, M.QBO_BILL), ("POST", "/journalentry?", 200, M.QBO_JOURNAL),
                    account_map={"office_supplies": "7", "wages": "60", "bank": "35"})
    vid = led.upsert_vendor("Synthetic Paper Co.", gst_number="123456782RT0001")
    bid = led.create_bill(vendor_id=vid, number="PS-1", bill_date="2026-10-20", due_date="2026-11-19",
                          subtotal_cents=10000, tax_cents=1300, category="office_supplies", itc_eligible=1,
                          currency="CAD", source="01.eml", status="approved")
    jid = led.post_journal_entry(date="2026-11-13", memo="Pay run", source="payroll",
                                 lines=[{"account": "wages", "debit_cents": 500000, "description": "gross"},
                                        {"account": "bank", "credit_cents": 500000, "description": "net"}])
    assert (vid, bid, jid) == ("77", "201", "310")
    _, vendor, bill, je = t.api_calls()
    assert vendor["body"]["TaxIdentifier"] == "123456782RT0001"
    assert bill["body"]["VendorRef"] == {"value": "77"}
    assert bill["body"]["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "7"}
    assert [ln["JournalEntryLineDetail"]["PostingType"] for ln in je["body"]["Line"]] == ["Debit", "Credit"]
    assert [ln["Amount"] for ln in je["body"]["Line"]] == [5000.0, 5000.0]


def test_qbo_unbalanced_journal_never_reaches_the_api():
    led, t, _ = qbo()
    with pytest.raises(ValueError):
        led.post_journal_entry(date="2026-11-13", memo="x", lines=[{"account": "a", "debit_cents": 1},
                                                                   {"account": "b", "credit_cents": 2}])
    assert t.calls == []


def test_qbo_retries_429_with_retry_after_and_same_requestid():
    led, t, sleeps = qbo(("GET", "/query", 200, M.QBO_QUERY_EMPTY),
                         ("POST", "/customer?", 429, M.QBO_THROTTLED, {"Retry-After": "7"}),
                         ("POST", "/customer?", 503, None),
                         ("POST", "/customer?", 200, M.QBO_CUSTOMER))
    assert led.upsert_customer("Acme Synthetic Ltd.", "", "ON") == "58"
    assert sleeps == [7.0, 2.0]
    rids = {params(c["url"])["requestid"] for c in t.api_calls()[1:]}
    assert len(rids) == 1


def test_qbo_validation_fault_raises_with_code():
    led, _, _ = qbo(("GET", "/query", 200, M.QBO_QUERY_EMPTY), ("POST", "/customer?", 400, M.QBO_FAULT))
    with pytest.raises(ApiError) as e:
        led.upsert_customer("Acme Synthetic Ltd.", "", "ON")
    assert e.value.status == 400 and "6140" in str(e.value)


def test_qbo_refreshes_once_on_401_and_reports_rotated_refresh_token():
    rotated = []
    t = FakeTransport(("POST", "tokens/bearer", 200, M.QBO_TOKEN),
                      ("GET", "/query", 401, None),
                      ("POST", "tokens/bearer", 200, M.QBO_TOKEN_ROTATED),
                      ("GET", "/query", 200, M.qbo_query("Vendor", [M.QBO_VENDOR["Vendor"]])))
    auth = OAuth2("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", "cid", "csecret", "mock-refresh-0",
                  transport=t, on_rotate=rotated.append)
    led = QuickBooksLedger(realm_id="1", auth=auth, transport=t, environment="production")
    assert led.upsert_vendor("Synthetic Paper Co.") == "77"
    assert rotated == ["mock-refresh-1", "mock-refresh-2"]
    token_call = t.calls[0]
    assert token_call["headers"]["Authorization"].startswith("Basic ")
    assert b"grant_type=refresh_token" in token_call["body"]
    assert t.calls[-1]["url"].startswith("https://quickbooks.api.intuit.com/v3/company/1/")
    assert t.calls[-1]["headers"]["Authorization"] == "Bearer mock-access-2"
    assert "csecret" not in repr(auth) and "mock-access" not in repr(auth)


def test_qbo_needs_item_and_account_config():
    led, _, _ = qbo()
    led.item_id = None
    with pytest.raises(AdapterConfigError):
        led.create_invoice(number="X", customer_id="1", period="p", issue_date="d", due_date="d", tax_code="",
                           tax_cents=0, lines=[{"desc": "x", "amount_cents": 1}])


# --- Xero ------------------------------------------------------------------------------------------

def test_xero_creates_contact_with_tenant_and_idempotency_headers():
    led, t = xero(("GET", "/Contacts?", 200, M.XERO_CONTACTS_EMPTY), ("PUT", "/Contacts?", 200, M.XERO_CONTACT))
    cid = led.upsert_customer("Acme Synthetic Ltd.", "ap@acme.example", "ON", "1 Example St")
    assert cid == "3ff6d40c-af9a-40a3-89ce-3c1556a25591"
    get, put = t.api_calls()
    assert params(get["url"])["where"] == 'Name=="Acme Synthetic Ltd."'
    assert put["url"].startswith("https://api.xero.com/api.xro/2.0/Contacts?")
    assert put["headers"]["xero-tenant-id"] == "tenant-123" and len(put["headers"]["Idempotency-Key"]) <= 128
    assert put["body"]["Contacts"][0]["Addresses"][0] == {"AddressType": "STREET", "AddressLine1": "1 Example St",
                                                          "Region": "ON", "Country": "Canada"}


def test_xero_invoice_bill_and_manual_journal():
    led, t = xero(("PUT", "/Invoices?", 200, M.XERO_INVOICE), ("PUT", "/Invoices?", 200, M.XERO_BILL),
                  ("PUT", "/ManualJournals?", 200, M.XERO_MANUAL_JOURNAL),
                  tax_type_map={"HST ON": "OUTPUT"}, account_map={"wages": "477", "bank": "090"})
    iid = led.create_invoice(number="INV-1001", customer_id="3ff6d40c", period="2026-10", issue_date="2026-10-31",
                             due_date="2026-11-30", tax_code="HST ON", tax_cents=13000, po_number="PO-7",
                             lines=[{"desc": "Consulting", "amount_cents": 100000}])
    bid = led.create_bill(vendor_id="7c3a0d9e", number="PS-1", bill_date="2026-10-20", due_date="2026-11-19",
                          subtotal_cents=10000, tax_cents=1300, category="office", source="01.eml")
    jid = led.post_journal_entry(date="2026-11-13", memo="Pay run",
                                 lines=[{"account": "wages", "debit_cents": 500000},
                                        {"account": "bank", "credit_cents": 500000}])
    assert iid.startswith("ed255415") and bid.startswith("4f7a612b") and jid.startswith("d312dd5e")
    inv, bill, mj = (c["body"] for c in t.api_calls())
    inv, bill, mj = inv["Invoices"][0], bill["Invoices"][0], mj["ManualJournals"][0]
    assert (inv["Type"], inv["Status"], inv["LineAmountTypes"], inv["Reference"]) == ("ACCREC", "DRAFT", "Exclusive", "PO-7")
    assert inv["LineItems"][0] == {"Description": "Consulting", "AccountCode": "200", "LineAmount": 1000.0,
                                   "TaxType": "OUTPUT"}
    assert (bill["Type"], bill["InvoiceNumber"], bill["LineItems"][0]["AccountCode"]) == ("ACCPAY", "PS-1", "400")
    assert [ln["LineAmount"] for ln in mj["JournalLines"]] == [5000.0, -5000.0]
    assert led.warnings == []


def test_xero_validation_errors_raise():
    led, _ = xero(("PUT", "/Invoices?", 200, M.XERO_INVOICE_INVALID))
    with pytest.raises(ApiError) as e:
        led.create_invoice(number="X", customer_id="c", period="p", issue_date="2026-10-31", due_date="2026-11-30",
                           tax_code="", tax_cents=0, lines=[{"desc": "x", "amount_cents": 1}])
    assert "not a valid code" in str(e.value)


def test_xero_status_must_be_draft_or_authorised():
    with pytest.raises(AdapterConfigError):
        xero(document_status="PAID")


# --- IMAP ------------------------------------------------------------------------------------------

class FakeIMAP:
    def __init__(self, messages):
        self.messages, self.log = messages, []

    def login(self, user, password):
        self.log.append(("login", user))
        return "OK", [b"logged in"]

    def authenticate(self, mech, cb):
        self.log.append(("authenticate", mech, cb(b"")))
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        self.log.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"1700000001"]

    def uid(self, cmd, *args):
        self.log.append(("uid", cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        return "OK", [(args[0] + b" (UID " + args[0] + b" BODY[] {10}", self.messages[args[0]]), b")"]

    def logout(self):
        self.log.append(("logout",))


def test_imap_reads_without_marking_seen_and_names_messages_by_uid():
    raw = build_eml(sender="v@x.test", to="ap@y.test", subject="Bill 7", date="Mon, 02 Nov 2026 09:00:00 -0500",
                    body="total 113.00", attachments=[("b.txt", "text/plain", b"113.00")])
    fake = FakeIMAP({b"41": raw, b"42": raw})
    mb = ImapMailbox(user="ap@y.test", password="app-pw", folder="finance/bills", connect=lambda h, p: fake)
    mails = mb.fetch()
    assert [m.subject for m in mails] == ["Bill 7", "Bill 7"]
    assert str(mails[0].path) == "imap/finance/bills/1700000001-41.eml"
    assert mails[0].attachments[0].data == b"113.00"
    assert ("select", "finance/bills", True) in fake.log
    fetches = [e for e in fake.log if e[:2] == ("uid", "FETCH")]
    assert all(e[3] == "(BODY.PEEK[])" for e in fetches) and fake.log[-1] == ("logout",)
    assert "app-pw" not in repr(mb)


def test_imap_xoauth2_and_limit():
    fake = FakeIMAP({b"1": b"Subject: a\r\n\r\nx", b"2": b"Subject: b\r\n\r\ny", b"3": b"Subject: c\r\n\r\nz"})
    mb = ImapMailbox(user="ap@y.test", oauth_token="mock-token", limit=2, connect=lambda h, p: fake)
    assert [m.subject for m in mb.fetch()] == ["b", "c"]
    assert fake.log[0] == ("authenticate", "XOAUTH2", b"user=ap@y.test\x01auth=Bearer mock-token\x01\x01")


def test_imap_from_env(monkeypatch):
    monkeypatch.setenv("AFO_IMAP_USER", "ap@y.test")
    monkeypatch.setenv("AFO_IMAP_PASSWORD", "pw")
    monkeypatch.setenv("AFO_IMAP_FOLDER", "Finance Inbox")
    mb = ImapMailbox.from_env(connect=lambda h, p: FakeIMAP({}))
    assert (mb.host, mb.port, mb.folder) == ("imap.gmail.com", 993, "Finance Inbox")
    assert mb.fetch() == []
