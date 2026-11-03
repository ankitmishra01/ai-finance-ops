"""Quarterly GST/HST return agent (Ontario registrant, regular method), plus the QST worksheet.

Dry run by default: classifies the quarter's ledger export, applies rules.yaml,
computes the GST34 lines, writes the working paper and the ready-to-file package
under out/gst_hst/, and prints a review table. It never files or pays.

  uv run python 04_gst_hst/run.py                      # dry run, rules extractor
  uv run python 04_gst_hst/run.py --approve            # review each proposal (y/n)
  uv run python 04_gst_hst/run.py --auto-approve       # demo: approve unflagged, hold flagged
  AFO_LLM_MODE=live uv run python 04_gst_hst/run.py    # Claude extractor (needs ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.approvals import ApprovalQueue  # noqa: E402
from shared.llm import FixtureMissing  # noqa: E402

from gst_hst import (DATA_DIR, Rules, compute_gst34, compute_qst, filing_deadline, fmt,  # noqa: E402
                     load_business, load_transactions, to_cents, treat)
from gst_hst_extract import classify_all, make_extractor  # noqa: E402
from gst_hst_report import write_all  # noqa: E402


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def build(data_dir: Path, extractor_name: str | None):
    rules = Rules()
    business = load_business(data_dir / "business.json")
    txns = load_transactions(data_dir / "transactions.csv")
    extractor = make_extractor(extractor_name, rules)
    classes = classify_all(extractor, txns)
    qst_registered = bool(business.get("qst_registered"))
    treated = treat(txns, classes, rules, qst_registered=qst_registered)
    lines = compute_gst34(treated, instalments_cents=to_cents(business.get("instalments_paid", "0")))
    qst = compute_qst(treated) if qst_registered else None
    due, eff = filing_deadline(date.fromisoformat(business["period_end"]), business["province"])
    return rules, business, extractor, treated, lines, due, eff, qst


def propose(queue: ApprovalQueue, treated, lines, business, due, eff, paths, qst=None) -> None:
    for t in treated:
        if not t.flags:
            continue
        x = t.txn
        queue.propose(
            "gst_hst_txn_review",
            f"{x.txn_id} {x.date} {x.description[:60]} ({fmt(x.amount_cents)}) -> {t.cls.category}",
            {"txn_id": x.txn_id, "category": t.cls.category, "supply_type": t.supply_type,
             "itc_treatment": t.itc_treatment, "place_of_supply": t.place, "lines": t.lines,
             "held_out": t.held_out, "confidence": t.cls.confidence, "qst_lines": t.qst_lines},
            flags=list(t.flags),
        )
    n_flagged = sum(1 for t in treated if t.flags)
    result = (f"owing {fmt(lines['115'])}" if lines["115"] else f"refund {fmt(lines['114'])}")
    queue.propose(
        "gst_hst_return",
        f"GST34 {business['period_start']}..{business['period_end']}: net tax {fmt(lines['109'])}, {result}, "
        f"due {due.isoformat()} - mark READY TO FILE (no filing is done)",
        {"lines_cents": lines, "due_date": due.isoformat(), "effective_due_date": eff.isoformat(),
         "package": {k: _rel(v) for k, v in paths.items()}},
        flags=[f"{n_flagged} transactions flagged; resolve them before filing"] if n_flagged else [],
    )
    if qst is not None:
        qres = f"owing {fmt(qst['209'])}" if qst["209"] >= 0 else f"refund {fmt(-qst['209'])}"
        queue.propose(
            "qst_return",
            f"QST (FPZ-500) {business['period_start']}..{business['period_end']}: collectible {fmt(qst['205'])}, "
            f"ITRs {fmt(qst['208'])}, {qres} - mark READY TO FILE (no filing is done)",
            {"lines_cents": qst, "qst_number": business.get("qst_number"),
             "package": _rel(paths["qst_worksheet_md"]) if "qst_worksheet_md" in paths else None},
            flags=[f"{n_flagged} transactions flagged; resolve them before filing"] if n_flagged else [],
        )


def print_table(treated, lines, due, eff, extractor_name, as_of: date, qst=None) -> None:
    print(f"\nGST/HST return (GST34), classifier: {extractor_name}")
    print("-" * 44)
    for k in ["101", "103", "104", "105", "106", "107", "108", "109", "110", "111", "205", "405", "113C", "114", "115"]:
        print(f"  line {k:<5} {fmt(lines[k]):>20}")
    if qst is not None:
        print("  QST (Revenu Quebec, FPZ-500)")
        for k in ["203", "205", "206", "207", "208", "209"]:
            print(f"  QST line {k:<5} {fmt(qst[k]):>16}")
    days = (eff - as_of).days
    print("-" * 44)
    print(f"  due {due}" + (f" (on time to {eff})" if eff != due else "") +
          (f", {days} days from {as_of}" if days >= 0 else f", OVERDUE by {-days} days at {as_of}"))
    flagged = [t for t in treated if t.flags]
    print(f"\n{len(flagged)} of {len(treated)} transactions flagged for review:")
    for t in flagged:
        print(f"  {t.txn.txn_id} {t.txn.description[:48]:<48} {fmt(t.txn.amount_cents):>12}  {t.flags[0][:70]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extractor", choices=["rules", "claude"], default=None,
                    help="default: claude if AFO_LLM_MODE is live/record, else rules")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true", help="review each proposal interactively")
    g.add_argument("--auto-approve", action="store_true", help="demo: approve unflagged proposals, hold flagged")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "gst_hst")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    a = ap.parse_args(argv)

    try:
        rules, business, extractor, treated, lines, due, eff, qst = build(a.data_dir, a.extractor)
    except FixtureMissing as e:
        print(f"Claude extractor has no recorded responses for this data.\n{e}", file=sys.stderr)
        return 2

    paths = write_all(a.out, treated, lines, rules, business, extractor.name, due, eff, qst)
    queue = ApprovalQueue()
    propose(queue, treated, lines, business, due, eff, paths, qst)
    print_table(treated, lines, due, eff, extractor.name, a.as_of, qst)

    if a.approve or a.auto_approve:
        queue.review(auto=a.auto_approve)
    else:
        print("\nDry run: proposals left pending. Re-run with --approve to review.")
    queue.write(a.out / "approvals.json")
    ret = next(p for p in queue.items if p.kind == "gst_hst_return")
    qret = next((p for p in queue.items if p.kind == "qst_return"), None)
    status = {"return_decision": ret.decision,
              "ready_to_file": ret.decision == "approved",
              "qst_return_decision": qret.decision if qret else None,
              "filed": False,
              "note": "This agent never files. A person files the approved figures with the CRA."}
    (a.out / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(f"\nReturn proposal: {ret.decision}. Outputs in {_rel(a.out)}/")
    for p in paths.values():
        print(f"  {p.name}")
    print("  approvals.json\n  status.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
