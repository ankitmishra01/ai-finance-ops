"""Month-end client invoicing agent.

  uv run python 01_invoicing/run.py                       # dry run for 2026-10 (default)
  uv run python 01_invoicing/run.py --period 2026-10 --approve       # review each proposal
  uv run python 01_invoicing/run.py --auto-approve                   # demo: approve unflagged only
  uv run python 01_invoicing/run.py --extractor claude               # needs AFO_LLM_MODE=live/record

Dry run writes out/invoicing/{plan.json,proposals.json} and DRAFT PDFs, and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from shared import ledger, llm  # noqa: E402
from shared.tax import fmt  # noqa: E402

import invoicing  # noqa: E402
from invoicing_extract import get_extractor  # noqa: E402
from invoicing_pdf import render_invoice  # noqa: E402


def review_table(q) -> str:
    rows = [("#", "kind", "decision", "summary", "flags")]
    for i, p in enumerate(q.items, 1):
        rows.append((str(i), p.kind, p.decision, p.summary, "; ".join(p.flags) or "-"))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    out = []
    for r in rows:
        out.append("  ".join(r[c].ljust(widths[c]) for c in range(4)) + "  " + r[4])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="2026-10")
    ap.add_argument("--issue-date", help="defaults to the 25th of the period")
    ap.add_argument("--extractor", choices=["rules", "claude"],
                    help="default: claude when AFO_LLM_MODE is live/record, else rules")
    ap.add_argument("--data", type=Path, default=invoicing.DATA)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "invoicing")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--approve", action="store_true", help="review each proposal interactively")
    mode.add_argument("--auto-approve", action="store_true", help="demo: approve unflagged, hold flagged")
    args = ap.parse_args(argv)

    issue = date.fromisoformat(args.issue_date) if args.issue_date else date.fromisoformat(f"{args.period}-25")
    extractor = get_extractor(args.extractor)
    ops = invoicing.load_ops(args.data / "ops.json")
    mails = invoicing.load_mail(args.data / "mailbox")
    args.out.mkdir(parents=True, exist_ok=True)
    ledger_path = args.out / "ledger.db"
    con = ledger.connect(ledger_path) if ledger_path.exists() else None
    if con is not None:
        # Never reuse an invoice number the ledger already holds.
        ops["supplier"]["next_invoice_seq"] = max(ops["supplier"]["next_invoice_seq"],
                                                  invoicing.next_seq_from_ledger(con))
    try:
        plan = invoicing.plan_month(ops, mails, args.period, issue, extractor)
    except llm.FixtureMissing as e:
        print(f"Claude extractor has no recorded response and AFO_LLM_MODE is replay.\n{e}\n"
              "Use --extractor rules, or set AFO_LLM_MODE=live with ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    if con is not None:
        invoicing.flag_ledger_duplicates(plan, con)

    q, deps = invoicing.build_queue(plan)
    (args.out / "plan.json").write_text(json.dumps(
        {"period": plan.period, "issue_date": plan.issue_date, "extractor": plan.extractor,
         "customers": [asdict(c) for c in plan.customers], "invoices": [i.to_dict() for i in plan.invoices],
         "not_billed": plan.not_billed}, indent=2, default=str))

    print(f"Invoicing {plan.period}, issue date {plan.issue_date}, extractor={plan.extractor}")
    print(f"{len(plan.invoices)} invoices, {sum(i.total_cents for i in plan.invoices) / 100:,.2f} CAD total; "
          f"{len(plan.not_billed)} clients not billed: "
          + ", ".join(f"{n['client_id']} ({n['reason']})" for n in plan.not_billed))
    print()

    if not (args.approve or args.auto_approve):
        for inv in plan.invoices:
            render_invoice(inv.to_dict(), plan.supplier, args.out / "drafts" / f"{inv.number}-DRAFT.pdf", draft=True)
        q.write(args.out / "proposals.json")
        print(review_table(q))
        print(f"\nDRY RUN: nothing written to the ledger. Draft PDFs and proposals in {args.out}/")
        print("Re-run with --approve to review, or --auto-approve to approve unflagged items.")
        return 0

    q.review(auto=args.auto_approve)
    invoicing.enforce_dependencies(q, deps)
    con = con or ledger.connect(ledger_path)
    written = invoicing.commit(q, plan, con, args.out)
    q.write(args.out / "proposals.json")
    print(review_table(q))
    approved_total = sum(p.payload["total_cents"] for p in q.items if p.kind == "invoice" and p.decision == "approved")
    print(f"\nUpserted {written['customers']} approved customer records and wrote {written['invoices']} invoices "
          f"({fmt(approved_total)}) to {ledger_path}; PDFs in {args.out / 'invoices'}/")
    held = [p for p in q.items if p.decision in {"held", "rejected"}]
    if held:
        print(f"{len(held)} items held or rejected; fix the flags and re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
