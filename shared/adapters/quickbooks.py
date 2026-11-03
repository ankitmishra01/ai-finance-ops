"""QuickBooks Online (Accounting API v3) adapter implementing shared.protocols.LedgerAdapter.

Opt-in: AFO_LEDGER=quickbooks. Nothing here has been run against a live QuickBooks company; the
unit tests use schema-shaped mock responses (tests/test_adapters.py). Run it against an Intuit
sandbox company before any production use.

References (Intuit Developer, QuickBooks Online Accounting API):
- Entities and request/response bodies:
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/customer
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/invoice
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/vendor
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/bill
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/journalentry
- Base URLs, query endpoint, sparse update, SyncToken and minor versions:
  https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api
  https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries
  https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/minor-versions
- OAuth 2.0 (token endpoint, refresh-token rotation):
  https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0
- Idempotent writes with the `requestid` query parameter (unique per company, max 50 chars):
  https://blogs.intuit.com/?p=30011
These pages are rendered client-side and could not be fetched as text when this file was written,
so field names follow the documented entity schemas as published there; confirm them against the
sandbox, especially the Canadian sales-tax fields (TaxCodeRef ids are company-specific).

Environment:
  QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REFRESH_TOKEN, QBO_REALM_ID   required
  QBO_ENVIRONMENT        sandbox (default) or production
  QBO_MINOR_VERSION      default 75
  QBO_ITEM_ID            Item used on invoice lines (QuickBooks requires an ItemRef on sales lines)
  QBO_EXPENSE_ACCOUNT_ID default expense Account for bill lines
  QBO_ACCOUNT_MAP        JSON {category or journal account name: QuickBooks Account Id}
  QBO_TAX_CODE_MAP       JSON {our tax_code, e.g. "HST ON" or "GST": QuickBooks TaxCode Id}
"""

from __future__ import annotations

import json

from shared.adapters.http import AdapterConfigError, JsonClient, OAuth2, Transport, env, idempotency_key, urllib_transport
from shared.protocols import check_balanced

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
CUSTOMER_FIELDS = frozenset({"name", "email", "province", "address"})


def _money(cents: int) -> float:
    return round(cents / 100, 2)


def _q(value: str) -> str:
    """Quote a string literal for the QuickBooks query language (backslash-escape single quotes)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class QuickBooksLedger:
    name = "quickbooks"

    def __init__(self, *, realm_id: str, auth: OAuth2, environment: str = "sandbox", minor_version: str = "75",
                 item_id: str | None = None, expense_account_id: str | None = None,
                 account_map: dict | None = None, tax_code_map: dict | None = None,
                 transport: Transport = urllib_transport, sleep=None) -> None:
        if environment not in BASE_URLS:
            raise AdapterConfigError("QBO_ENVIRONMENT must be 'sandbox' or 'production'")
        self.base = f"{BASE_URLS[environment]}/v3/company/{realm_id}"
        self.minor_version = minor_version
        self.item_id, self.expense_account_id = item_id, expense_account_id
        self.account_map, self.tax_code_map = account_map or {}, tax_code_map or {}
        kw = {"sleep": sleep} if sleep else {}
        self.http = JsonClient(auth=auth, transport=transport, **kw)
        self.warnings: list[str] = []

    @classmethod
    def from_env(cls, transport: Transport = urllib_transport, **kw) -> "QuickBooksLedger":
        e = env("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REFRESH_TOKEN", "QBO_REALM_ID",
                optional=("QBO_ENVIRONMENT", "QBO_MINOR_VERSION", "QBO_ITEM_ID", "QBO_EXPENSE_ACCOUNT_ID",
                          "QBO_ACCOUNT_MAP", "QBO_TAX_CODE_MAP"))
        auth = OAuth2(TOKEN_URL, e["QBO_CLIENT_ID"], e["QBO_CLIENT_SECRET"], e["QBO_REFRESH_TOKEN"], transport=transport)
        return cls(realm_id=e["QBO_REALM_ID"], auth=auth, environment=e["QBO_ENVIRONMENT"] or "sandbox",
                   minor_version=e["QBO_MINOR_VERSION"] or "75", item_id=e["QBO_ITEM_ID"],
                   expense_account_id=e["QBO_EXPENSE_ACCOUNT_ID"],
                   account_map=json.loads(e["QBO_ACCOUNT_MAP"] or "{}"),
                   tax_code_map=json.loads(e["QBO_TAX_CODE_MAP"] or "{}"), transport=transport, **kw)

    # --- plumbing ---------------------------------------------------------------------------------

    def _url(self, path: str, **params) -> str:
        from urllib.parse import urlencode

        return f"{self.base}/{path}?{urlencode({'minorversion': self.minor_version, **params})}"

    def _create(self, entity: str, body: dict) -> dict:
        """POST /v3/company/{realmId}/{entity}. The requestid makes a retried create return the
        original object instead of a duplicate (Intuit: unique per company, at most 50 chars)."""
        rid = idempotency_key("qbo", entity, body)
        out = self.http.request("POST", self._url(entity.lower(), requestid=rid), body=body)
        return out[entity]

    def _query_one(self, entity: str, where: str) -> dict | None:
        """GET /v3/company/{realmId}/query?query=select * from <Entity> where ..."""
        out = self.http.request("GET", self._url("query", query=f"select * from {entity} where {where}"))
        rows = out.get("QueryResponse", {}).get(entity, [])
        return rows[0] if rows else None

    def _tax_ref(self, tax_code: str | None) -> dict | None:
        if not tax_code:
            return None
        return {"value": str(self.tax_code_map.get(tax_code, tax_code))}

    def _account(self, key: str | None, default: str | None, what: str) -> dict:
        acct = self.account_map.get(key or "", default)
        if not acct:
            raise AdapterConfigError(f"no QuickBooks Account Id for {what} {key!r}: set QBO_ACCOUNT_MAP "
                                     "or QBO_EXPENSE_ACCOUNT_ID")
        return {"value": str(acct)}

    # --- LedgerAdapter ----------------------------------------------------------------------------

    def upsert_customer(self, name, email, province, address=""):
        hit = self._query_one("Customer", f"DisplayName = {_q(name)}")
        if hit:
            return hit["Id"]
        body = {"DisplayName": name, "CompanyName": name}
        if email:
            body["PrimaryEmailAddr"] = {"Address": email}
        if address or province:
            body["BillAddr"] = {"Line1": address, "CountrySubDivisionCode": province, "Country": "CA"}
        return self._create("Customer", body)["Id"]

    def update_customer(self, customer_id, **fields):
        bad = set(fields) - CUSTOMER_FIELDS
        if bad:
            raise ValueError(f"cannot update customer field(s): {', '.join(sorted(bad))}")
        if not fields:
            return 0
        # A sparse update must carry the current SyncToken (optimistic concurrency).
        current = self.http.request("GET", self._url(f"customer/{customer_id}"))["Customer"]
        body = {"Id": str(customer_id), "SyncToken": current["SyncToken"], "sparse": True}
        if "name" in fields:
            body["DisplayName"] = fields["name"]
        if "email" in fields:
            body["PrimaryEmailAddr"] = {"Address": fields["email"]}
        if "address" in fields or "province" in fields:
            addr = dict(current.get("BillAddr") or {})
            addr.pop("Id", None)
            if "address" in fields:
                addr["Line1"] = fields["address"]
            if "province" in fields:
                addr["CountrySubDivisionCode"] = fields["province"]
            body["BillAddr"] = addr
        self.http.request("POST", self._url("customer"), body=body)
        return 1

    def create_invoice(self, *, number, customer_id, period, issue_date, due_date, lines, tax_code, tax_cents,
                       po_number=None, currency="CAD"):
        if not self.item_id:
            raise AdapterConfigError("QBO_ITEM_ID is required: QuickBooks sales lines need an ItemRef")
        tax_ref = self._tax_ref(tax_code)
        qbo_lines = []
        for ln in lines:
            detail = {"ItemRef": {"value": str(self.item_id)}}
            if ln.get("qty") is not None and ln.get("unit_cents") is not None:
                detail["Qty"] = ln["qty"]
                detail["UnitPrice"] = _money(ln["unit_cents"])
            if tax_ref:
                detail["TaxCodeRef"] = tax_ref
            qbo_lines.append({"DetailType": "SalesItemLineDetail", "Amount": _money(ln["amount_cents"]),
                              "Description": ln.get("desc", ""), "SalesItemLineDetail": detail})
        body = {"DocNumber": number, "TxnDate": issue_date, "DueDate": due_date,
                "CustomerRef": {"value": str(customer_id)}, "CurrencyRef": {"value": currency},
                "GlobalTaxCalculation": "TaxExcluded", "Line": qbo_lines,
                "PrivateNote": f"period {period}" + (f"; PO {po_number}" if po_number else "")}
        inv = self._create("Invoice", body)
        # QuickBooks computes tax from the tax code; compare with what the agent computed.
        qbo_tax = (inv.get("TxnTaxDetail") or {}).get("TotalTax")
        if qbo_tax is not None and round(float(qbo_tax) * 100) != tax_cents:
            self.warnings.append(f"invoice {number}: QuickBooks tax {qbo_tax} != agent tax {tax_cents / 100:.2f}")
        return inv["Id"]

    def upsert_vendor(self, name, email="", gst_number="", terms_days=30):
        hit = self._query_one("Vendor", f"DisplayName = {_q(name)}")
        if hit:
            return hit["Id"]
        body = {"DisplayName": name, "CompanyName": name}
        if email:
            body["PrimaryEmailAddr"] = {"Address": email}
        if gst_number:
            # Vendor.TaxIdentifier holds the supplier's tax registration number.
            body["TaxIdentifier"] = gst_number
        return self._create("Vendor", body)["Id"]

    def create_bill(self, **f):
        tax_ref = self._tax_ref(f.get("tax_code"))
        detail = {"AccountRef": self._account(f.get("category"), self.expense_account_id, "bill category")}
        if tax_ref:
            detail["TaxCodeRef"] = tax_ref
        body = {"VendorRef": {"value": str(f["vendor_id"])}, "DocNumber": f.get("number"),
                "TxnDate": f.get("bill_date"), "DueDate": f.get("due_date"),
                "CurrencyRef": {"value": f.get("currency") or "CAD"}, "GlobalTaxCalculation": "TaxExcluded",
                "Line": [{"DetailType": "AccountBasedExpenseLineDetail", "Amount": _money(f["subtotal_cents"]),
                          "Description": f.get("category") or "", "AccountBasedExpenseLineDetail": detail}],
                "PrivateNote": f"source {f.get('source')}; ITC {'eligible' if f.get('itc_eligible') else 'not claimed'}"}
        return self._create("Bill", {k: v for k, v in body.items() if v is not None})["Id"]

    def post_journal_entry(self, *, date, memo, lines, source=""):
        check_balanced(lines)
        qbo_lines = []
        for ln in lines:
            debit = int(ln.get("debit_cents") or 0)
            qbo_lines.append({
                "DetailType": "JournalEntryLineDetail",
                "Amount": _money(debit or int(ln["credit_cents"])),
                "Description": ln.get("description", ""),
                "JournalEntryLineDetail": {"PostingType": "Debit" if debit else "Credit",
                                           "AccountRef": self._account(ln["account"], None, "journal account")},
            })
        body = {"TxnDate": date, "PrivateNote": f"{memo} [{source}]" if source else memo, "Line": qbo_lines}
        return self._create("JournalEntry", body)["Id"]

    def set_status(self, table, row_id, status):
        # Objects are only created after a person approves them, so "approved" needs no further write.
        # QuickBooks has no draft state for invoices or bills to move them out of.
        if status != "approved":
            raise NotImplementedError(f"QuickBooks adapter cannot set status {status!r}")

    def commit(self):
        """Each QuickBooks call commits on its own."""
