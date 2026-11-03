"""A small SQLite stand-in for an accounting platform (QuickBooks, Xero, Wave).

Modules talk to this through plain functions so a real adapter can replace it
later without touching agent logic. Amounts are integer cents throughout.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists customers (
  id integer primary key, name text unique, email text, province text, address text
);
create table if not exists invoices (
  id integer primary key, number text unique, customer_id integer, period text,
  issue_date text, due_date text, subtotal_cents integer, tax_cents integer,
  tax_code text, status text default 'draft', lines_json text
);
create table if not exists vendors (
  id integer primary key, name text unique, email text, gst_number text, terms_days integer
);
create table if not exists bills (
  id integer primary key, vendor_id integer, number text, bill_date text, due_date text,
  subtotal_cents integer, tax_cents integer, itc_eligible integer, category text,
  status text default 'draft', source text
);
create table if not exists transactions (
  id integer primary key, date text, description text, amount_cents integer,
  account text, category text, tax_code text, evidence_path text
);
create table if not exists pay_runs (
  id integer primary key, period_start text, period_end text, pay_date text,
  status text default 'draft', summary_json text
);
"""

# Columns added after the first schema. Each runs as ALTER TABLE ADD COLUMN only when the column
# is missing, so a ledger.db written by an older version opens and upgrades in place.
MIGRATIONS = [
    ("bills", "currency", "text default 'CAD'"),
    ("bills", "total_cents", "integer"),
    ("bills", "other_tax_cents", "integer default 0"),
    ("invoices", "po_number", "text"),
    ("invoices", "currency", "text default 'CAD'"),
]

# Columns each update_* helper may change. Keys are never interpolated unless listed here.
CUSTOMER_FIELDS = frozenset({"name", "email", "province", "address"})
TRANSACTION_FIELDS = frozenset({"date", "description", "amount_cents", "account", "category",
                                "tax_code", "evidence_path"})


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate(con)
    return con


def columns(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"pragma table_info({table})")}


def migrate(con) -> list[str]:
    """Add any MIGRATIONS columns the database lacks. Idempotent; returns what was added."""
    added = []
    for table, col, decl in MIGRATIONS:
        if col not in columns(con, table):
            con.execute(f"alter table {table} add column {col} {decl}")
            added.append(f"{table}.{col}")
    if added:
        con.commit()
    return added


def _update(con, table: str, allowed: frozenset, row_id: int, fields: dict) -> int:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {table} column(s): {', '.join(sorted(bad))}")
    if not fields:
        return 0
    sets = ", ".join(f"{k} = ?" for k in fields)
    return con.execute(f"update {table} set {sets} where id = ?", (*fields.values(), row_id)).rowcount


def update_customer(con, customer_id: int, **fields) -> int:
    """Change whitelisted columns of one customer. Returns rows updated (0 or 1)."""
    return _update(con, "customers", CUSTOMER_FIELDS, customer_id, fields)


def update_transaction(con, txn_id: int, **fields) -> int:
    """Change whitelisted columns of one transaction. Returns rows updated (0 or 1)."""
    return _update(con, "transactions", TRANSACTION_FIELDS, txn_id, fields)


def upsert_customer(con, name: str, email: str, province: str, address: str = "") -> int:
    row = con.execute("select id from customers where name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "insert into customers (name, email, province, address) values (?, ?, ?, ?)",
        (name, email, province, address),
    )
    return cur.lastrowid


def create_invoice(con, *, number, customer_id, period, issue_date, due_date, lines, tax_code, tax_cents,
                   po_number=None, currency="CAD") -> int:
    subtotal = sum(l["amount_cents"] for l in lines)
    cur = con.execute(
        "insert into invoices (number, customer_id, period, issue_date, due_date, subtotal_cents,"
        " tax_cents, tax_code, lines_json, po_number, currency) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (number, customer_id, period, issue_date, due_date, subtotal, tax_cents, tax_code, json.dumps(lines),
         po_number, currency),
    )
    return cur.lastrowid


def upsert_vendor(con, name: str, email: str = "", gst_number: str = "", terms_days: int = 30) -> int:
    row = con.execute("select id from vendors where name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "insert into vendors (name, email, gst_number, terms_days) values (?, ?, ?, ?)",
        (name, email, gst_number, terms_days),
    )
    return cur.lastrowid


def create_bill(con, **f) -> int:
    cols = ", ".join(f)
    marks = ", ".join("?" for _ in f)
    return con.execute(f"insert into bills ({cols}) values ({marks})", tuple(f.values())).lastrowid


def add_transaction(con, **f) -> int:
    cols = ", ".join(f)
    marks = ", ".join("?" for _ in f)
    return con.execute(f"insert into transactions ({cols}) values ({marks})", tuple(f.values())).lastrowid


def set_status(con, table: str, row_id: int, status: str) -> None:
    assert table in {"invoices", "bills", "pay_runs"}
    con.execute(f"update {table} set status = ? where id = ?", (status, row_id))
