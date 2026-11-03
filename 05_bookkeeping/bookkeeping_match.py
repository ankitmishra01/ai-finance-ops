"""Deterministic receipt-to-transaction matching.

No model is involved here. Inputs are extracted receipt fields and bank/card lines;
the output is a list of links, each with the evidence for it (merchant similarity,
amount difference, date gap) and any flags a person should look at.

Order of passes:
  1. Duplicate receipts (same merchant, date, total and document number) are set aside.
  2. One-to-one matches: date within +/-5 days (of the document date, or of a scheduled
     debit date printed on a bill), amount within tolerance (or within an
     FX rate band for foreign-currency receipts), merchant names similar enough.
  3. One-to-many: an unmatched receipt whose total equals the sum of 2-3 unmatched
     payments to the same payee within a wider window (split or instalment payments).
  3b. Amount-only: exact amount within 2 days, names unalike (numbered companies), and
     the pairing is unique both ways. Linked, but flagged for a person to confirm.
  4. Amount mismatches: same merchant, date in window, amount within 25% but outside
     tolerance (a tip added after the receipt was printed, a partial refund). Linked,
     but flagged.
Everything left over is an exception: transactions with no evidence, orphan receipts.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

DATE_WINDOW_DAYS = 5
SPLIT_WINDOW = (-5, 30)               # days after the receipt date that instalments may land
AMOUNT_TOL_CENTS = 2                  # rounding between processor and bank
AMOUNT_TOL_PCT = 0.002
FX_BANDS = {"USD": (1.30, 1.50), "EUR": (1.40, 1.70), "GBP": (1.65, 1.95)}  # CAD per unit incl. card FX fee
MISMATCH_PCT = 0.25
MIN_SIMILARITY = 0.6
AMOUNT_ONLY_DAYS = 2                  # exact-amount fallback when names do not resemble each other

PROCESSOR_PREFIXES = re.compile(
    r"^(?:SQ \*|TST\* ?|SP \* ?|DASHLY \*|PAYPAL \*|PP\*|POS PURCHASE |VISA DEBIT |PREAUTH DEBIT |PAD |"
    r"BILL PMT |INTERAC E-TRF SENT |ONLINE BANKING PMT )", re.I)
NOISE_TOKENS = {
    "inc", "ltd", "llc", "co", "corp", "company", "the", "and", "of", "via", "ca", "on", "bc", "qc", "ab", "us",
    "toronto", "ottawa", "mississauga", "montreal", "vancouver", "san", "francisco", "toront", "mktp", "help",
    "billing", "accounts", "receipts", "pad", "subscr", "storage", "test", "io", "com",
    # generic business words: they say nothing about which business it is
    "studio", "cafe", "bistro", "shop", "goods", "services", "office", "supply", "hotel", "suites",
    "roasters", "coffee", "kitchen", "grill", "bakery", "fuel", "ads", "rail", "mobile", "properties",
    "marketplace", "design", "suite", "cpa", "trip", "mutual", "insurance", "ins", "hydro", "fibre", "print",
    "drive", "accounting", "lot",
}


def norm_tokens(name: str) -> list[str]:
    s = PROCESSOR_PREFIXES.sub("", name.strip())
    s = re.sub(r"\*\S*", " ", s)                      # card reference codes: *2K4L19RT3
    s = re.sub(r"#\s*\d+", " ", s)                    # store numbers
    s = s.replace("&", " and ").lower()
    toks = re.findall(r"[a-z]+", s)
    return [t for t in toks if len(t) > 1 and t not in NOISE_TOKENS]


def norm_name(name: str) -> str:
    return " ".join(norm_tokens(name))


def merchant_similarity(receipt_merchant: str, descriptor: str) -> float:
    """0..1. Token overlap with prefix matching (descriptors get truncated), backed by difflib."""
    a, b = norm_tokens(receipt_merchant), norm_tokens(descriptor)
    if not a or not b:
        # Only generic words on one side; fall back to raw string similarity.
        return SequenceMatcher(None, receipt_merchant.lower(), descriptor.lower()).ratio() * 0.8
    hits = 0
    for t in a:
        if any(u == t or (len(u) >= 4 and t.startswith(u)) or (len(t) >= 4 and u.startswith(t)) for u in b):
            hits += 1
    token_score = hits / len(a)
    seq = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    return round(max(token_score, seq), 3)


@dataclass
class Receipt:
    receipt_id: str
    merchant: str
    date: date
    total_cents: int                  # negative for refunds
    currency: str = "CAD"
    gst_hst_cents: int = 0
    gst_number: str | None = None
    is_receipt: bool = True
    invoice_number: str | None = None
    summary: str = ""
    payment_date: date | None = None   # scheduled debit / due date printed on a bill


def days_apart(r: Receipt, t: Txn) -> int:
    """Gap to the nearer of the document date and any scheduled payment date on it."""
    gaps = [abs((t.date - r.date).days)]
    if r.payment_date:
        gaps.append(abs((t.date - r.payment_date).days))
    return min(gaps)


@dataclass
class Txn:
    txn_id: str
    date: date
    account: str
    description: str
    amount_cents: int                 # negative = money out


@dataclass
class Link:
    receipt_id: str
    txn_ids: list[str]
    kind: str                         # exact | fx | split | amount_only | mismatch
    score: float
    similarity: float
    amount_diff_cents: int
    days_apart: int
    flags: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    links: list[Link]
    unmatched_txns: list[str]
    orphan_receipts: list[str]
    duplicate_receipts: dict[str, str]          # duplicate id -> original id
    not_receipts: list[str]                     # quotes/estimates: never matched
    duplicate_charges: list[tuple[str, str]]


def amount_ok(r: Receipt, t: Txn) -> tuple[bool, int, list[str]]:
    """Does the transaction amount fit the receipt? Returns (ok, diff in cents, flags)."""
    charged = -t.amount_cents                    # purchases are negative on the statement
    if (r.total_cents < 0) != (charged < 0):     # refund must meet money coming back
        return False, abs(charged - r.total_cents), []
    if r.currency != "CAD":
        lo, hi = FX_BANDS.get(r.currency, (0, 0))
        if r.total_cents == 0:
            return False, abs(charged), []
        rate = charged / r.total_cents
        ok = lo <= rate <= hi
        return ok, 0 if ok else abs(charged - r.total_cents), [f"FX {r.currency} {abs(r.total_cents) / 100:.2f} "
                                                               f"at implied {rate:.4f} CAD"] if ok else []
    diff = charged - r.total_cents
    tol = max(AMOUNT_TOL_CENTS, int(abs(r.total_cents) * AMOUNT_TOL_PCT))
    return abs(diff) <= tol, diff, []


def _score(sim: float, days: int, diff: int, total: int) -> float:
    amt = 1.0 - min(1.0, abs(diff) / max(1, abs(total)))
    return round(0.5 * sim + 0.3 * amt + 0.2 * (1 - min(days, 10) / 10), 4)


def find_duplicate_receipts(receipts: list[Receipt]) -> dict[str, str]:
    seen: dict[tuple, str] = {}
    dups = {}
    for r in sorted(receipts, key=lambda x: x.receipt_id):
        k = (norm_name(r.merchant), r.date, r.total_cents, r.invoice_number or "")
        if k in seen:
            dups[r.receipt_id] = seen[k]
        else:
            seen[k] = r.receipt_id
    return dups


def find_duplicate_charges(txns: list[Txn]) -> list[tuple[str, str]]:
    seen: dict[tuple, str] = {}
    out = []
    for t in txns:
        if t.amount_cents > 0:
            continue
        k = (t.date, t.account, t.description, t.amount_cents)
        if k in seen:
            out.append((seen[k], t.txn_id))
        else:
            seen[k] = t.txn_id
    return out


def match(receipts: list[Receipt], txns: list[Txn]) -> MatchResult:
    dups = find_duplicate_receipts(receipts)
    not_receipts = [r.receipt_id for r in receipts if not r.is_receipt and r.receipt_id not in dups]
    pool = [r for r in receipts if r.receipt_id not in dups and r.is_receipt]
    tx_by_id = {t.txn_id: t for t in txns}
    used_t: set[str] = set()
    used_r: set[str] = set()
    links: list[Link] = []

    # Pass 2: one-to-one, best score first (stable ordering for ties).
    cands = []
    for r in pool:
        for t in txns:
            days = days_apart(r, t)
            if days > DATE_WINDOW_DAYS:
                continue
            ok, diff, flags = amount_ok(r, t)
            if not ok:
                continue
            sim = merchant_similarity(r.merchant, t.description)
            if sim < MIN_SIMILARITY:
                continue
            kind = "fx" if r.currency != "CAD" else "exact"
            cands.append((_score(sim, days, diff, r.total_cents), r.receipt_id, t.txn_id, kind, sim, diff, days, flags))
    for sc, rid, tid, kind, sim, diff, days, flags in sorted(cands, key=lambda c: (-c[0], c[1], c[2])):
        if rid in used_r or tid in used_t:
            continue
        used_r.add(rid)
        used_t.add(tid)
        links.append(Link(rid, [tid], kind, sc, sim, diff, days, list(flags)))

    # Pass 3: one receipt, several payments.
    for r in pool:
        if r.receipt_id in used_r or r.currency != "CAD":
            continue
        near = [t for t in txns if t.txn_id not in used_t
                and SPLIT_WINDOW[0] <= (t.date - r.date).days <= SPLIT_WINDOW[1]
                and (t.amount_cents < 0) == (r.total_cents > 0)
                and merchant_similarity(r.merchant, t.description) >= MIN_SIMILARITY]
        found = None
        for n in (2, 3):
            for combo in itertools.combinations(near, n):
                if abs(-sum(t.amount_cents for t in combo) - r.total_cents) <= AMOUNT_TOL_CENTS:
                    found = combo
                    break
            if found:
                break
        if found:
            ids = [t.txn_id for t in found]
            used_r.add(r.receipt_id)
            used_t.update(ids)
            sim = min(merchant_similarity(r.merchant, t.description) for t in found)
            parts = " + ".join(f"{-t.amount_cents / 100:,.2f}" for t in found)
            links.append(Link(r.receipt_id, ids, "split", _score(sim, 0, 0, r.total_cents), sim, 0,
                              max(abs((t.date - r.date).days) for t in found),
                              [f"split payment: {parts} = {r.total_cents / 100:,.2f}"]))

    # Pass 3b: exact amount, close date, but the names do not resemble each other (numbered
    # companies, parent-company descriptors). Only when the pairing is unique both ways; always flagged.
    for r in pool:
        if r.receipt_id in used_r or r.currency != "CAD" or r.total_cents == 0:
            continue
        fits = [t for t in txns if t.txn_id not in used_t and days_apart(r, t) <= AMOUNT_ONLY_DAYS
                and amount_ok(r, t)[0] and amount_ok(r, t)[1] == 0]
        if len(fits) != 1:
            continue
        t = fits[0]
        rivals = [o for o in pool if o.receipt_id not in used_r and o.receipt_id != r.receipt_id
                  and o.currency == "CAD" and days_apart(o, t) <= AMOUNT_ONLY_DAYS and amount_ok(o, t)[0]]
        if rivals:
            continue
        sim = merchant_similarity(r.merchant, t.description)
        used_r.add(r.receipt_id)
        used_t.add(t.txn_id)
        links.append(Link(r.receipt_id, [t.txn_id], "amount_only", _score(sim, days_apart(r, t), 0, r.total_cents),
                          sim, 0, days_apart(r, t),
                          [f"merchant name differs: receipt '{r.merchant}' vs descriptor '{t.description}' "
                           f"(exact amount, {days_apart(r, t)} day(s) apart); confirm"]))

    # Pass 4: right merchant and date, wrong amount.
    for r in pool:
        if r.receipt_id in used_r or r.currency != "CAD" or r.total_cents == 0:
            continue
        best = None
        for t in txns:
            if t.txn_id in used_t or days_apart(r, t) > DATE_WINDOW_DAYS:
                continue
            if (t.amount_cents < 0) != (r.total_cents > 0):
                continue
            sim = merchant_similarity(r.merchant, t.description)
            diff = -t.amount_cents - r.total_cents
            if sim >= 0.75 and abs(diff) <= abs(r.total_cents) * MISMATCH_PCT:
                key = (sim, -abs(diff))
                if best is None or key > best[0]:
                    best = (key, t, sim, diff)
        if best:
            _, t, sim, diff = best
            used_r.add(r.receipt_id)
            used_t.add(t.txn_id)
            links.append(Link(r.receipt_id, [t.txn_id], "mismatch", _score(sim, days_apart(r, t), diff,
                                                                            r.total_cents), sim, diff,
                              days_apart(r, t),
                              [f"amount mismatch: receipt {r.total_cents / 100:,.2f}, charged "
                               f"{-t.amount_cents / 100:,.2f} (diff {diff / 100:+,.2f})"]))

    links.sort(key=lambda l: (min(tx_by_id[i].date for i in l.txn_ids), l.txn_ids[0]))
    return MatchResult(
        links=links,
        unmatched_txns=[t.txn_id for t in txns if t.txn_id not in used_t],
        orphan_receipts=[r.receipt_id for r in pool if r.receipt_id not in used_r],
        duplicate_receipts=dups,
        not_receipts=not_receipts,
        duplicate_charges=find_duplicate_charges(txns),
    )
