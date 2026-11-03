"""The two boundaries an agent crosses: the accounting ledger and the mailbox.

Agents are written against these Protocols. The defaults are the SQLite stand-in
(`shared.ledger.SQLiteLedger`) and the .eml folder (`shared.mail.EmlMailbox`); the opt-in
adapters in `shared/adapters/` (QuickBooks Online, Xero, IMAP/Gmail) implement the same methods.
`shared.adapters.get_ledger()` and `get_mailbox()` pick one from AFO_LEDGER / AFO_MAILBOX.

Amounts are integer cents everywhere. Ids are whatever the platform uses: SQLite row ids are
ints, QuickBooks ids are numeric strings, Xero ids are UUID strings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

Id = int | str


@runtime_checkable
class LedgerAdapter(Protocol):
    name: str

    def upsert_customer(self, name: str, email: str, province: str, address: str = "") -> Id:
        """Return the id of the customer with this name, creating it if absent."""

    def update_customer(self, customer_id: Id, **fields) -> int:
        """Change whitelisted fields (name, email, province, address). Returns records updated."""

    def create_invoice(self, *, number: str, customer_id: Id, period: str, issue_date: str, due_date: str,
                       lines: list[dict], tax_code: str, tax_cents: int, po_number: str | None = None,
                       currency: str = "CAD") -> Id:
        """Create a sales invoice. Each line has desc and amount_cents (qty, unit_cents optional)."""

    def upsert_vendor(self, name: str, email: str = "", gst_number: str = "", terms_days: int = 30) -> Id:
        """Return the id of the vendor with this name, creating it if absent."""

    def create_bill(self, **fields) -> Id:
        """Create a payable. Fields as ledger.bills: vendor_id, number, bill_date, due_date,
        subtotal_cents, tax_cents, other_tax_cents, total_cents, currency, itc_eligible, category,
        status, source."""

    def post_journal_entry(self, *, date: str, memo: str, lines: list[dict], source: str = "") -> Id:
        """Post a balanced journal. Each line: account, debit_cents or credit_cents, description."""

    def set_status(self, table: str, row_id: Id, status: str) -> None:
        """Record an approval status where the platform has one."""

    def commit(self) -> None:
        """Make writes durable (SQLite commit; a no-op for HTTP platforms, which commit per call)."""


@runtime_checkable
class Mailbox(Protocol):
    name: str

    def fetch(self) -> list:
        """Return the messages to process, as shared.mail.Mail objects, oldest first."""


def check_balanced(lines: list[dict]) -> tuple[int, int]:
    """Validate journal lines; returns (debits, credits) in cents. Raises ValueError if unbalanced."""
    if len(lines) < 2:
        raise ValueError("a journal entry needs at least two lines")
    debits = credits = 0
    for i, ln in enumerate(lines):
        d, c = int(ln.get("debit_cents") or 0), int(ln.get("credit_cents") or 0)
        if d < 0 or c < 0 or (d and c) or not (d or c):
            raise ValueError(f"line {i}: exactly one of debit_cents or credit_cents must be positive")
        if not ln.get("account"):
            raise ValueError(f"line {i}: account is required")
        debits += d
        credits += c
    if debits != credits:
        raise ValueError(f"journal does not balance: debits {debits} != credits {credits}")
    return debits, credits
