"""Month-end bookkeeping: match receipts to bank/card lines, propose categories, file evidence.

Dry run by default: writes proposals, staged evidence and an exceptions report under
out/bookkeeping/ and prints a review table. Nothing reaches the ledger unless approved.

  uv run python 05_bookkeeping/run.py                    # dry run, rules extractor
  uv run python 05_bookkeeping/run.py --approve          # review each proposal (y/N)
  uv run python 05_bookkeeping/run.py --auto-approve     # demo: approve all unflagged, hold flagged
  AFO_LLM_MODE=live ANTHROPIC_API_KEY=... uv run python 05_bookkeeping/run.py   # Claude extractor
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from shared import ledger  # noqa: E402
from shared.llm import FixtureMissing  # noqa: E402

import bookkeeping as bk  # noqa: E402
from bookkeeping_extract import make_extractor  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extractor", choices=["rules", "claude"], default=None,
                    help="default: claude when AFO_LLM_MODE is live/record, else rules")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true", help="review proposals interactively")
    g.add_argument("--auto-approve", action="store_true", help="approve unflagged proposals, hold flagged ones")
    ap.add_argument("--data", type=Path, default=bk.DATA)
    ap.add_argument("--out", type=Path, default=bk.OUT)
    args = ap.parse_args(argv)

    chart = bk.load_chart(args.data)
    ex = make_extractor(args.extractor, chart)
    try:
        res = bk.run(ex, args.data, args.out, chart)
    except FixtureMissing as e:
        print(f"Claude extractor has no recorded response for this input.\n{e}\n"
              "Use --extractor rules, or set AFO_LLM_MODE=live (or record) with ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    period = json.loads((args.data / "labels.json").read_text()).get("period", "") if (args.data / "labels.json").exists() else ""
    mode = "approve" if args.approve else "auto-approve" if args.auto_approve else "dry run"
    print(f"Bookkeeping {period} | extractor: {ex.name} | mode: {mode}")
    print(f"{len(res.txns)} transactions, {len(res.receipts)} emails, {len(res.matches.links)} links, "
          f"{sum(len(l.txn_ids) for l in res.matches.links)} transactions evidenced\n")

    db = args.out / "ledger.db"
    if db.exists():
        db.unlink()
    con = ledger.connect(db)               # rebuilt from the synthetic month on every run
    ids = bk.load_ledger(con, res.txns)

    if args.approve or args.auto_approve:
        res.queue.review(auto=args.auto_approve)
        done = bk.apply_approved(con, res.queue, ids, args.out)
    else:
        done = None

    bk.write_outputs(res, args.out, period)
    print(bk.review_table(res.queue))
    print()
    for key, rows in res.exceptions.items():
        print(f"  {bk.EX_TITLES[key]}: {len(rows)}")
    counts = {}
    for p in res.queue.items:
        counts[p.decision] = counts.get(p.decision, 0) + 1
    print(f"\nProposals: {len(res.queue.items)} {counts}")
    if done is not None:
        print(f"Ledger updated: {done}")
    else:
        print("Dry run: ledger not changed. Re-run with --approve or --auto-approve to apply.")
    print(f"Outputs: {args.out}/ (proposals.json, exceptions.md, exceptions.csv, evidence/, ledger.db)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
