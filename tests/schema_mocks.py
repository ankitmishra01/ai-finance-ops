"""SCHEMA-SHAPED MOCKS. NOT RECORDED RESPONSES.

Every response body in this file was written by hand to match the response schema the platform
documents. None was captured from a real QuickBooks, Xero, Gmail or Claude account; ids, names
and amounts are invented and synthetic. They test that the adapters build the right requests and
read the documented fields, not that the platforms behave this way today.

Sources for the shapes:
- QuickBooks Online Accounting API v3 entity references (Customer, Invoice, Vendor, Bill,
  JournalEntry), query responses ({"QueryResponse": {...}, "time": ...}) and Fault errors:
  https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/invoice and siblings.
- Intuit OAuth 2.0 token response:
  https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0
- Xero Accounting API: the response examples in Xero's OpenAPI spec,
  https://github.com/XeroAPI/Xero-OpenAPI/blob/master/xero_accounting.yaml (createContacts,
  createInvoices, createManualJournals), trimmed to the fields the adapter reads.
- claude CLI headless JSON result (`claude -p --output-format json`): type, subtype, is_error,
  result, usage, modelUsage, total_cost_usd.
"""

LABEL = "schema-shaped mock, not a recorded response"
TIME = "2026-11-03T09:00:00.000-08:00"

# --- Intuit ---------------------------------------------------------------------------------------

QBO_TOKEN = {"token_type": "bearer", "access_token": "mock-access-1", "refresh_token": "mock-refresh-1",
             "expires_in": 3600, "x_refresh_token_expires_in": 8726400}
QBO_TOKEN_ROTATED = {**QBO_TOKEN, "access_token": "mock-access-2", "refresh_token": "mock-refresh-2"}

QBO_QUERY_EMPTY = {"QueryResponse": {}, "time": TIME}


def qbo_query(entity: str, rows: list[dict]) -> dict:
    return {"QueryResponse": {entity: rows, "startPosition": 1, "maxResults": len(rows)}, "time": TIME}


QBO_CUSTOMER = {"Customer": {
    "Id": "58", "SyncToken": "0", "DisplayName": "Acme Synthetic Ltd.", "CompanyName": "Acme Synthetic Ltd.",
    "PrimaryEmailAddr": {"Address": "ap@acme.example"}, "Active": True, "Balance": 0,
    "BillAddr": {"Id": "91", "Line1": "1 Example St", "CountrySubDivisionCode": "ON", "Country": "CA"},
    "MetaData": {"CreateTime": TIME, "LastUpdatedTime": TIME}}, "time": TIME}

QBO_INVOICE = {"Invoice": {
    "Id": "130", "SyncToken": "0", "DocNumber": "INV-1001", "TxnDate": "2026-10-31", "DueDate": "2026-11-30",
    "CustomerRef": {"value": "58", "name": "Acme Synthetic Ltd."}, "CurrencyRef": {"value": "CAD"},
    "GlobalTaxCalculation": "TaxExcluded", "TotalAmt": 1130.0, "Balance": 1130.0,
    "TxnTaxDetail": {"TotalTax": 130.0, "TaxLine": [{"Amount": 130.0, "DetailType": "TaxLineDetail",
                                                      "TaxLineDetail": {"TaxRateRef": {"value": "17"},
                                                                        "PercentBased": True, "TaxPercent": 13,
                                                                        "NetAmountTaxable": 1000.0}}]},
    "Line": [{"Id": "1", "LineNum": 1, "Amount": 1000.0, "DetailType": "SalesItemLineDetail",
              "SalesItemLineDetail": {"ItemRef": {"value": "1"}, "TaxCodeRef": {"value": "13"}}},
             {"Amount": 1000.0, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}}],
    "MetaData": {"CreateTime": TIME, "LastUpdatedTime": TIME}}, "time": TIME}

QBO_VENDOR = {"Vendor": {"Id": "77", "SyncToken": "0", "DisplayName": "Synthetic Paper Co.", "Active": True,
                         "MetaData": {"CreateTime": TIME, "LastUpdatedTime": TIME}}, "time": TIME}

QBO_BILL = {"Bill": {"Id": "201", "SyncToken": "0", "DocNumber": "PS-1", "TxnDate": "2026-10-20",
                     "VendorRef": {"value": "77"}, "TotalAmt": 113.0, "Balance": 113.0,
                     "Line": [{"Id": "1", "Amount": 100.0, "DetailType": "AccountBasedExpenseLineDetail",
                               "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}}}],
                     "MetaData": {"CreateTime": TIME, "LastUpdatedTime": TIME}}, "time": TIME}

QBO_JOURNAL = {"JournalEntry": {"Id": "310", "SyncToken": "0", "TxnDate": "2026-11-13", "Adjustment": False,
                                "Line": [], "MetaData": {"CreateTime": TIME, "LastUpdatedTime": TIME}},
               "time": TIME}

QBO_FAULT = {"Fault": {"Error": [{"Message": "Duplicate Document Number Error",
                                  "Detail": "Duplicate Document Number Error : You must specify a different number.",
                                  "code": "6140", "element": ""}], "type": "ValidationFault"}, "time": TIME}

QBO_THROTTLED = {"Fault": {"Error": [{"Message": "message=ThrottleExceeded", "code": "003001"}],
                           "type": "ThrottleExceeded"}, "time": TIME}

# --- Xero -----------------------------------------------------------------------------------------

XERO_TOKEN = {"id_token": "mock-id", "access_token": "mock-xero-access", "expires_in": 1800,
              "token_type": "Bearer", "refresh_token": "mock-xero-refresh",
              "scope": "accounting.transactions accounting.contacts offline_access"}


def _envelope(key: str, items: list[dict]) -> dict:
    return {"Id": "e997d6d7-6dad-4458-beb8-d9c1bf7f2edf", "Status": "OK", "ProviderName": "Synthetic Test App",
            "DateTimeUTC": "/Date(1793000000000)/", key: items}


XERO_CONTACTS_EMPTY = _envelope("Contacts", [])
XERO_CONTACT = _envelope("Contacts", [{
    "ContactID": "3ff6d40c-af9a-40a3-89ce-3c1556a25591", "ContactStatus": "ACTIVE", "Name": "Acme Synthetic Ltd.",
    "EmailAddress": "ap@acme.example", "Addresses": [{"AddressType": "STREET", "Region": "ON", "Country": "Canada"}],
    "IsSupplier": False, "IsCustomer": False, "HasValidationErrors": False}])
XERO_INVOICE = _envelope("Invoices", [{
    "Type": "ACCREC", "InvoiceID": "ed255415-e141-4150-aab7-89c3bbbb851c", "InvoiceNumber": "INV-1001",
    "Reference": "PO-7", "Status": "DRAFT", "LineAmountTypes": "Exclusive", "CurrencyCode": "CAD",
    "Contact": {"ContactID": "3ff6d40c-af9a-40a3-89ce-3c1556a25591"},
    "LineItems": [{"Description": "Consulting", "LineAmount": 1000.0, "AccountCode": "200", "TaxType": "OUTPUT"}],
    "SubTotal": 1000.0, "TotalTax": 130.0, "Total": 1130.0, "HasErrors": False, "StatusAttributeString": "OK"}])
XERO_BILL = _envelope("Invoices", [{
    "Type": "ACCPAY", "InvoiceID": "4f7a612b-fdcc-4d33-90fa-a9f6bc6db32f", "InvoiceNumber": "PS-1",
    "Status": "DRAFT", "Contact": {"ContactID": "7c3a0d9e-2b55-4c1e-9b0e-1f2d3c4b5a69"}, "SubTotal": 100.0,
    "TotalTax": 13.0, "Total": 113.0, "HasErrors": False}])
XERO_MANUAL_JOURNAL = _envelope("ManualJournals", [{
    "ManualJournalID": "d312dd5e-a53e-46d1-9d51-c569ef4570b7", "Narration": "Pay run", "Status": "DRAFT",
    "LineAmountTypes": "NoTax", "JournalLines": [], "ShowOnCashBasisReports": True}])
XERO_INVOICE_INVALID = _envelope("Invoices", [{
    "Type": "ACCREC", "InvoiceID": "00000000-0000-0000-0000-000000000000", "HasErrors": True,
    "ValidationErrors": [{"Message": "Account code '999' is not a valid code for this document."}]}])

# --- claude CLI -----------------------------------------------------------------------------------


def cli_result(result_text: str, *, usage: bool = True, model: str = "claude-sonnet-5-5") -> dict:
    out = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200, "num_turns": 1,
           "result": result_text, "session_id": "00000000-0000-4000-8000-000000000000"}
    if usage:
        out["usage"] = {"input_tokens": 900, "output_tokens": 60, "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0}
        out["modelUsage"] = {model: {"inputTokens": 900, "outputTokens": 60, "cacheReadInputTokens": 0,
                                     "cacheCreationInputTokens": 0, "costUSD": 0.0024}}
        out["total_cost_usd"] = 0.0024
    return out
