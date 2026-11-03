"""Semi-monthly pay run for a small Ontario employer (synthetic data).

  uv run python 03_payroll/run.py                   # dry run: proposals + review table only
  uv run python 03_payroll/run.py --approve         # review each pay line interactively
  uv run python 03_payroll/run.py --auto-approve    # demo: approve unflagged lines, hold flagged
  uv run python 03_payroll/run.py --extractor claude  # read emails with Claude (needs AFO_LLM_MODE=live|record)

Outputs go to out/payroll/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from payroll import DATA, build_pay_run, load_inputs, propose, totals  # noqa: E402
from payroll_extract import default_extractor  # noqa: E402
from payroll_outputs import (pd7a_summary, post_journal, write_cpa005, write_payslip,  # noqa: E402
                             write_pd7a)
from shared import ledger  # noqa: E402
from shared.approvals import ApprovalQueue  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

OUT = ROOT / "out" / "payroll"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true", help="review each line interactively")
    g.add_argument("--auto-approve", action="store_true", help="approve unflagged lines, hold flagged ones")
    ap.add_argument("--extractor", choices=["rules", "claude"], default=None)
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    inputs = load_inputs(args.data)
    roster = [e.name for e in inputs.employees]
    extractor = default_extractor(args.extractor)

    adjustments = []
    for mail in read_mailbox(args.data / "inbox"):
        for a in extractor.extract(mail, roster=roster, period_start=inputs.period.start):
            a["source"] = mail.path.name
            adjustments.append(a)

    run = build_pay_run(inputs, adjustments)
    queue = ApprovalQueue()
    propose(run, queue)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "adjustments.json").write_text(json.dumps(adjustments, indent=2) + "\n")
    _write_register(run, out / "pay_register.csv")

    per = inputs.period
    print(f"Pay run {per.start}..{per.end}, pay date {per.pay_date}  (extractor: {extractor.name})")
    print(f"{len(adjustments)} adjustments read from {len(read_mailbox(args.data / 'inbox'))} emails\n")
    _print_table(run)

    if not (args.approve or args.auto_approve):
        queue.write(out / "approvals.json")
        print(f"\nDry run. Nothing paid, filed or posted. Proposals in {_rel(out)}/")
        print("Re-run with --approve to review, or --auto-approve for a demo.")
        return 0

    queue.review(auto=args.auto_approve)
    queue.write(out / "approvals.json")
    approved_ids = {p.payload["employee_id"] for p in queue.items if p.kind == "pay_line" and p.decision == "approved"}
    lines = [l for l in run.lines if l.employee.id in approved_ids]
    held = [p for p in queue.items if p.decision != "approved"]

    print(f"\nApproved {len(lines)} pay lines; {len(held)} held or rejected:")
    for p in held:
        print(f"  - [{p.decision}] {p.summary.split(':')[0]}: {'; '.join(p.flags)}")
    if not lines:
        print("Nothing approved; no outputs produced.")
        return 0

    for l in lines:
        write_payslip(l, run, out / "payslips" / f"{per.pay_date}_{l.employee.id}.pdf")
    summary = pd7a_summary(lines, run)
    write_pd7a(summary, out)
    write_cpa005(lines, run, out)
    con = ledger.connect(out / "ledger.db")
    prior = con.execute("select id from pay_runs where period_start = ? and status = 'approved'",
                        (per.start.isoformat(),)).fetchone()
    if prior:
        pr_id = prior["id"]
        print(f"\nWARNING: pay run #{pr_id} for this period is already posted; journal not posted again.")
    else:
        pr_id = post_journal(con, lines, run)

    print(f"\nPayslips:      {len(lines)} PDFs in {_rel(out / 'payslips')}")
    print(f"PD7A summary:  ${summary['amount_due']} due to the CRA by {summary['due_date']} ({_rel(out / 'pd7a_summary.txt')})")
    print(f"Bank file:     {_rel(out / 'SAMPLE_NOT_FOR_UPLOAD_cpa005.txt')} (CPA 005, SAMPLE, not for upload)")
    print(f"Journal:       pay run #{pr_id} posted to {_rel(out / 'ledger.db')}")
    return 0


def _rel(p: Path) -> str:
    return os.path.relpath(p)


def _write_register(run, path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee_id", "name", "gross", "cpp", "cpp2", "ei", "federal_tax", "ontario_tax", "net",
                    "employer_cpp", "employer_cpp2", "employer_ei", "flags"])
        for l in run.lines:
            d = l.deductions
            w.writerow([l.employee.id, l.employee.name, l.gross, d.cpp, d.cpp2, d.ei, d.federal_tax, d.ontario_tax,
                        l.net, d.employer_cpp, d.employer_cpp2, d.employer_ei, " | ".join(l.flags)])


def _print_table(run) -> None:
    hdr = f"{'ID':<4} {'Employee':<20} {'Gross':>10} {'CPP+2':>8} {'EI':>7} {'Tax':>9} {'Net':>10}  Flags"
    print(hdr)
    print("-" * len(hdr))
    for l in run.lines:
        d = l.deductions
        print(f"{l.employee.id:<4} {l.employee.name[:20]:<20} {l.gross:>10,.2f} {d.cpp + d.cpp2:>8,.2f} "
              f"{d.ei:>7,.2f} {d.income_tax:>9,.2f} {l.net:>10,.2f}  {'; '.join(l.flags)}")
    t = totals(run.lines)
    print("-" * len(hdr))
    print(f"{'':<4} {'TOTAL':<20} {t['gross']:>10,.2f} {t['cpp'] + t['cpp2']:>8,.2f} {t['ei']:>7,.2f} "
          f"{t['income_tax']:>9,.2f} {t['net']:>10,.2f}")
    print(f"Remittance if all lines approved: ${t['remittance']:,.2f}")
    for x in run.exceptions:
        print(f"EXCEPTION: {x['type']} for '{x.get('employee_name')}' ({x.get('source')}): {x['reason']}")


if __name__ == "__main__":
    raise SystemExit(main())
