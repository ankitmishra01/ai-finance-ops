"""Xero Accounting API adapter implementing shared.protocols.LedgerAdapter.

Opt-in: AFO_LEDGER=xero. Nothing here has been run against a live Xero organisation; the unit
tests use mock responses shaped from the examples in Xero's published OpenAPI spec. Run it
against the Xero Demo Company before any production use.

References:
- Xero's official OpenAPI description of the Accounting API, which this file follows for paths,
  headers and field names: https://github.com/XeroAPI/Xero-OpenAPI/blob/master/xero_accounting.yaml
  (server https://api.xero.com/api.xro/2.0; operations getContacts, createContacts,
  updateOrCreateContacts, createInvoices, createManualJournals; header `xero-tenant-id` required;
  header `Idempotency-Key` "allows you to safely retry requests without the risk of duplicate
  processing", 128 characters max; ManualJournalLine.LineAmount "Debits are positive, credits are
  negative value"; Invoice.Type ACCREC = sales invoice, ACCPAY = bill).
- Human-readable docs: https://developer.xero.com/documentation/api/accounting/contacts ,
  .../invoices , .../manualjournals
- OAuth 2.0 (token endpoint https://identity.xero.com/connect/token, refresh-token rotation):
  https://developer.xero.com/documentation/guides/oauth2/auth-flow
- Rate limits (HTTP 429 with Retry-After): https://developer.xero.com/documentation/guides/oauth2/limits

Agents propose and people approve in this repo, so invoices, bills and journals are created in
Xero as DRAFT by default: a second person authorises them in Xero (see docs/production.md on
segregation of duties). XERO_DOCUMENT_STATUS=AUTHORISED changes that.

Environment:
  XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REFRESH_TOKEN, XERO_TENANT_ID   required
  XERO_SALES_ACCOUNT_CODE     account code for invoice lines
  XERO_EXPENSE_ACCOUNT_CODE   default account code for bill lines
  XERO_ACCOUNT_MAP            JSON {category or journal account name: Xero AccountCode}
  XERO_TAX_TYPE_MAP           JSON {our tax_code: Xero TaxType}
  XERO_DOCUMENT_STATUS        DRAFT (default) or AUTHORISED
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from shared.adapters.http import AdapterConfigError, ApiError, JsonClient, OAuth2, Transport, env, idempotency_key, urllib_transport
from shared.protocols import check_balanced

TOKEN_URL = "https://identity.xero.com/connect/token"
BASE_URL = "https://api.xero.com/api.xro/2.0"
CUSTOMER_FIELDS = frozenset({"name", "email", "province", "address"})


def _money(cents: int) -> float:
    return round(cents / 100, 2)


def _where_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class XeroLedger:
    name = "xero"

    def __init__(self, *, tenant_id: str, auth: OAuth2, sales_account_code: str | None = None,
                 expense_account_code: str | None = None, account_map: dict | None = None,
                 tax_type_map: dict | None = None, document_status: str = "DRAFT",
                 transport: Transport = urllib_transport, sleep=None) -> None:
        if document_status not in ("DRAFT", "AUTHORISED"):
            raise AdapterConfigError("XERO_DOCUMENT_STATUS must be DRAFT or AUTHORISED")
        self.tenant_id = tenant_id
        self.sales_account_code, self.expense_account_code = sales_account_code, expense_account_code
        self.account_map, self.tax_type_map = account_map or {}, tax_type_map or {}
        self.status = document_status
        kw = {"sleep": sleep} if sleep else {}
        self.http = JsonClient(auth=auth, transport=transport, **kw)
        self.warnings: list[str] = []

    @classmethod
    def from_env(cls, transport: Transport = urllib_transport, **kw) -> "XeroLedger":
        e = env("XERO_CLIENT_ID", "XERO_CLIENT_SECRET", "XERO_REFRESH_TOKEN", "XERO_TENANT_ID",
                optional=("XERO_SALES_ACCOUNT_CODE", "XERO_EXPENSE_ACCOUNT_CODE", "XERO_ACCOUNT_MAP",
                          "XERO_TAX_TYPE_MAP", "XERO_DOCUMENT_STATUS"))
        auth = OAuth2(TOKEN_URL, e["XERO_CLIENT_ID"], e["XERO_CLIENT_SECRET"], e["XERO_REFRESH_TOKEN"],
                      transport=transport)
        return cls(tenant_id=e["XERO_TENANT_ID"], auth=auth, sales_account_code=e["XERO_SALES_ACCOUNT_CODE"],
                   expense_account_code=e["XERO_EXPENSE_ACCOUNT_CODE"],
                   account_map=json.loads(e["XERO_ACCOUNT_MAP"] or "{}"),
                   tax_type_map=json.loads(e["XERO_TAX_TYPE_MAP"] or "{}"),
                   document_status=e["XERO_DOCUMENT_STATUS"] or "DRAFT", transport=transport, **kw)

    # --- plumbing ---------------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
              key: str | None = None) -> dict:
        url = f"{BASE_URL}/{path}" + (f"?{urlencode(params)}" if params else "")
        headers = {"xero-tenant-id": self.tenant_id}
        if key:
            headers["Idempotency-Key"] = key
        return self.http.request(method, url, body=body, headers=headers)

    def _put_one(self, collection: str, item: dict) -> dict:
        """PUT /{Collection} with one item; raises if Xero returns per-item ValidationErrors."""
        out = self._call("PUT", collection, {collection: [item]}, params={"summarizeErrors": "false"},
                         key=idempotency_key("xero", collection, item))
        got = out[collection][0]
        if got.get("HasErrors") or got.get("ValidationErrors"):
            raise ApiError(200, {"ValidationErrors": got.get("ValidationErrors")}, f"{BASE_URL}/{collection}")
        self.warnings += [w.get("Message", "") for w in got.get("Warnings") or []]
        return got

    def _tax_type(self, tax_code: str | None) -> str | None:
        return self.tax_type_map.get(tax_code or "", None)

    def _code(self, key: str | None, default: str | None, what: str) -> str:
        code = self.account_map.get(key or "", default)
        if not code:
            raise AdapterConfigError(f"no Xero AccountCode for {what} {key!r}: set XERO_ACCOUNT_MAP or a default code")
        return str(code)

    def _contact(self, name: str, email: str, extra: dict) -> str:
        found = self._call("GET", "Contacts", params={"where": f"Name=={_where_str(name)}"}).get("Contacts", [])
        if found:
            return found[0]["ContactID"]
        item = {"Name": name, **({"EmailAddress": email} if email else {}), **extra}
        return self._put_one("Contacts", item)["ContactID"]

    # --- LedgerAdapter ----------------------------------------------------------------------------

    def upsert_customer(self, name, email, province, address=""):
        extra = {}
        if address or province:
            extra["Addresses"] = [{"AddressType": "STREET", "AddressLine1": address, "Region": province,
                                   "Country": "Canada"}]
        return self._contact(name, email, extra)

    def update_customer(self, customer_id, **fields):
        bad = set(fields) - CUSTOMER_FIELDS
        if bad:
            raise ValueError(f"cannot update customer field(s): {', '.join(sorted(bad))}")
        if not fields:
            return 0
        item = {"ContactID": str(customer_id)}
        if "name" in fields:
            item["Name"] = fields["name"]
        if "email" in fields:
            item["EmailAddress"] = fields["email"]
        if "address" in fields or "province" in fields:
            addr = {"AddressType": "STREET"}
            if "address" in fields:
                addr["AddressLine1"] = fields["address"]
            if "province" in fields:
                addr["Region"] = fields["province"]
            item["Addresses"] = [addr]
        # POST /Contacts is updateOrCreateContacts: with a ContactID it updates that contact.
        self._call("POST", "Contacts", {"Contacts": [item]}, key=idempotency_key("xero", "update", item))
        return 1

    def create_invoice(self, *, number, customer_id, period, issue_date, due_date, lines, tax_code, tax_cents,
                       po_number=None, currency="CAD"):
        code = self._code("sales", self.sales_account_code, "sales")
        tax_type = self._tax_type(tax_code)
        items = []
        for ln in lines:
            li = {"Description": ln.get("desc", ""), "AccountCode": code, "LineAmount": _money(ln["amount_cents"])}
            if ln.get("qty") is not None and ln.get("unit_cents") is not None:
                li["Quantity"], li["UnitAmount"] = ln["qty"], _money(ln["unit_cents"])
            if tax_type:
                li["TaxType"] = tax_type
            items.append(li)
        inv = self._put_one("Invoices", {
            "Type": "ACCREC", "Contact": {"ContactID": str(customer_id)}, "InvoiceNumber": number,
            "Reference": po_number or period, "Date": issue_date, "DueDate": due_date,
            "LineAmountTypes": "Exclusive", "CurrencyCode": currency, "Status": self.status, "LineItems": items})
        if inv.get("TotalTax") is not None and round(float(inv["TotalTax"]) * 100) != tax_cents:
            self.warnings.append(f"invoice {number}: Xero tax {inv['TotalTax']} != agent tax {tax_cents / 100:.2f}")
        return inv["InvoiceID"]

    def upsert_vendor(self, name, email="", gst_number="", terms_days=30, qst_number=""):
        # Xero's Contact has a single TaxNumber field; the QST number has no slot of its own,
        # so it is not sent. Keep it in the local queue payloads for the FPZ-500 working papers.
        extra = {"TaxNumber": gst_number} if gst_number else {}
        if terms_days:
            extra["PaymentTerms"] = {"Bills": {"Day": int(terms_days), "Type": "DAYSAFTERBILLDATE"}}
        return self._contact(name, email, extra)

    def create_bill(self, **f):
        li = {"Description": f.get("category") or "", "Quantity": 1, "UnitAmount": _money(f["subtotal_cents"]),
              "AccountCode": self._code(f.get("category"), self.expense_account_code, "bill category")}
        tax_type = self._tax_type(f.get("tax_code"))
        if tax_type:
            li["TaxType"] = tax_type
        bill = self._put_one("Invoices", {
            # For ACCPAY, InvoiceNumber is the supplier's own invoice number.
            "Type": "ACCPAY", "Contact": {"ContactID": str(f["vendor_id"])}, "InvoiceNumber": f.get("number"),
            "Reference": f.get("source"), "Date": f.get("bill_date"), "DueDate": f.get("due_date"),
            "LineAmountTypes": "Exclusive", "CurrencyCode": f.get("currency") or "CAD", "Status": self.status,
            "LineItems": [li]})
        return bill["InvoiceID"]

    def post_journal_entry(self, *, date, memo, lines, source=""):
        check_balanced(lines)
        jl = [{"LineAmount": _money(int(ln.get("debit_cents") or 0) - int(ln.get("credit_cents") or 0)),
               "AccountCode": self._code(ln["account"], None, "journal account"),
               "Description": ln.get("description", "")} for ln in lines]
        mj = self._put_one("ManualJournals", {"Narration": f"{memo} [{source}]" if source else memo, "Date": date,
                                              "Status": self.status, "JournalLines": jl})
        return mj["ManualJournalID"]

    def set_status(self, table, row_id, status):
        # Approval in this repo means "create it in Xero"; authorising is a separate person's step in Xero.
        if status != "approved":
            raise NotImplementedError(f"Xero adapter cannot set status {status!r}")

    def commit(self):
        """Each Xero call commits on its own."""
