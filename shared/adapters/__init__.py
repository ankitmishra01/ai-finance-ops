"""Choose the ledger and mailbox from the environment. The defaults are the SQLite stand-in and .eml.

    AFO_LEDGER=sqlite (default) | quickbooks | xero
    AFO_MAILBOX=eml (default) | imap

Each real adapter reads its credentials from its own environment variables (see the module
docstrings) and fails fast with AdapterConfigError naming what is missing, never what is set.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from shared.adapters.http import AdapterConfigError, ApiError
from shared.protocols import LedgerAdapter, Mailbox

__all__ = ["AdapterConfigError", "ApiError", "get_ledger", "get_mailbox"]


def get_ledger(con: sqlite3.Connection | None = None, path: str | Path = ":memory:") -> LedgerAdapter:
    kind = os.environ.get("AFO_LEDGER", "sqlite")
    if kind == "sqlite":
        from shared.ledger import SQLiteLedger

        return SQLiteLedger(con, path)
    if kind == "quickbooks":
        from shared.adapters.quickbooks import QuickBooksLedger

        return QuickBooksLedger.from_env()
    if kind == "xero":
        from shared.adapters.xero import XeroLedger

        return XeroLedger.from_env()
    raise AdapterConfigError(f"AFO_LEDGER must be sqlite, quickbooks or xero, not {kind!r}")


def get_mailbox(folder: str | Path) -> Mailbox:
    """`folder` is the .eml directory used by the default; the IMAP adapter reads AFO_IMAP_FOLDER."""
    kind = os.environ.get("AFO_MAILBOX", "eml")
    if kind == "eml":
        from shared.mail import EmlMailbox

        return EmlMailbox(Path(folder))
    if kind == "imap":
        from shared.adapters.imap import ImapMailbox

        return ImapMailbox.from_env()
    raise AdapterConfigError(f"AFO_MAILBOX must be eml or imap, not {kind!r}")
