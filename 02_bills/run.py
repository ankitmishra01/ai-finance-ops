"""Accounts payable intake from the shared finance mailbox.

    uv run python 02_bills/run.py                     # dry run: proposals + AP aging, no ledger writes
    uv run python 02_bills/run.py --approve           # review each bill at the prompt
    uv run python 02_bills/run.py --auto-approve      # demo: approve clean bills, hold flagged ones
    uv run python 02_bills/run.py --extractor claude  # use Claude (needs AFO_LLM_MODE=live/record)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from bills import BUCKETS, bucket_totals, ledger_bills, post_approved, process, propose_all, write_aging  # noqa: E402
from bills_extract import get_extractor  # noqa: E402
from shared import ledger  # noqa: E402
from shared.approvals import ApprovalQueue  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402
from shared.tax import fmt  # noqa: E402

DATA = ROOT / "data" / "synthetic" / "bills"
OUT = ROOT / "out" / "bills"


def main(argv: list[str] | None = None) -> int:
    meta = json.loads((DATA / "labels.json").read_text())
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", type=Path, default=DATA / "inbox")
    ap.add_argument("--extractor", choices=["rules", "claude"], default=None,
                    help="default: rules, or claude when AFO_LLM_MODE is live/record")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.fromisoformat(meta["as_of"]),
                    help="date for due-date buckets (default: the synthetic set's date, for reproducible output)")
    ap.add_argument("--province", default=meta.get("recipient_province", "ON"), help="recipient's province")
    ap.add_argument("--ledger", type=Path, default=OUT / "ledger.db")
    ap.add_argument("--out", type=Path, default=OUT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--approve", action="store_true", help="review each proposal interactively")
    mode.add_argument("--auto-approve", action="store_true", help="demo mode: flagged items are held")
    args = ap.parse_args(argv)

    extractor = get_extractor(args.extractor)
    mails = read_mailbox(args.inbox)
    extracted = [(m.path.name, extractor.extract(m)) for m in mails]

    counts: dict[str, int] = {}
    for _, e in extracted:
        counts[e.doc_type] = counts.get(e.doc_type, 0) + 1
    print(f"Read {len(mails)} messages with the {extractor.name} extractor: "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))

    con = ledger.connect(args.ledger) if (args.approve or args.auto_approve) else ledger.connect()
    reviews = process(extracted, as_of=args.as_of, recipient_province=args.province, existing=ledger_bills(con),
                      qst_registered=meta.get("recipient_qst_registered", False))

    queue = ApprovalQueue()
    propose_all(queue, reviews)

    print()
    print(f"{'file':<8} {'vendor':<30} {'invoice':<14} {'total':>14} {'due':<10} {'bucket':<14} {'ITC':>9}  flags")
    for r in reviews:
        e = r.e
        print(f"{r.source:<8} {(e.vendor_name or '?')[:30]:<30} {(e.invoice_number or '?')[:14]:<14} "
              f"{e.currency + ' ' + fmt(e.total_cents or 0):>14} {str(r.due or '?'):<10} {r.bucket or '?':<14} "
              f"{fmt(r.itc.claimable_cents):>9}  {'; '.join(r.flags) or '-'}")
    for src, e in extracted:
        if e.doc_type != "bill":
            print(f"{src:<8} skipped: {e.doc_type}")

    if args.approve or args.auto_approve:
        approved = queue.review(auto=args.auto_approve)
        ids = post_approved(con, [p.payload for p in approved])
        print(f"\nPosted {len(ids)} bills to {args.ledger}; "
              f"{sum(p.decision == 'held' for p in queue.items)} held, "
              f"{sum(p.decision == 'rejected' for p in queue.items)} rejected.")
    else:
        print("\nDry run: nothing written to the ledger. Re-run with --approve to review.")

    decisions = {p.payload["source"]: p.decision for p in queue.items}
    queue.write(args.out / "proposals.json")
    csv_path, md_path = write_aging(reviews, decisions, args.out, args.as_of)

    print(f"\nAP aging as of {args.as_of} (exact duplicates excluded):")
    for cur, b in sorted(bucket_totals(reviews).items()):
        print(f"  {cur}: " + ", ".join(f"{k} {fmt(b[k])}" for k in BUCKETS))
    not_dup = [r for r in reviews if not any(f.startswith("duplicate:") for f in r.flags)]
    other = {}
    for r in not_dup:
        kind = r.payload()["other_tax_kind"]
        if kind:
            other[kind] = other.get(kind, 0) + r.e.other_tax_cents
    if other:
        print("Provincial tax on bills: " + ", ".join(f"{k} {fmt(v)}" for k, v in sorted(other.items()))
              + f"; QST input tax refunds claimable {fmt(sum(r.qst_itr.claimable_cents for r in not_dup))}"
              + " (PST and RST are a cost)")
    flagged = sum(bool(r.flags) for r in reviews)
    print(f"\n{len(reviews)} bills proposed, {flagged} flagged for a person.")
    print(f"Wrote {csv_path.relative_to(ROOT) if csv_path.is_relative_to(ROOT) else csv_path}, "
          f"{md_path.name}, proposals.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
