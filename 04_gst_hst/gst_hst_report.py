"""Working paper (markdown + CSV), the QST worksheet and the ready-to-file package. Pure formatting."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from gst_hst import LINE_ORDER, QST_LINE_ORDER, Rules, Treated, fmt

TRACED = ["101", "103", "106", "107", "405"]
QST_TRACED = ["203", "206", "207"]


def _esc(s: str) -> str:
    return s.replace("|", "\\|")


def write_csv(treated: list[Treated], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "date", "description", "amount", "net", "gst_hst", "supplier_bn", "qst",
                    "supplier_qst", "category", "supply_type", "place_of_supply", "rate", "itc_treatment",
                    "confidence", *[f"line_{k}" for k in TRACED], *[f"qst_line_{k}" for k in QST_TRACED],
                    "held_out", "flags"])
        for t in treated:
            x = t.txn
            w.writerow([x.txn_id, x.date.isoformat(), x.description, f"{x.amount_cents / 100:.2f}",
                        f"{x.net_cents / 100:.2f}", f"{x.tax_cents / 100:.2f}", x.supplier_bn,
                        f"{x.qst_cents / 100:.2f}", x.supplier_qst, t.cls.category,
                        t.supply_type, t.place, f"{t.rate:.2f}", t.itc_treatment, f"{t.cls.confidence:.2f}",
                        *[f"{t.lines.get(k, 0) / 100:.2f}" for k in TRACED],
                        *[f"{t.qst_lines.get(k, 0) / 100:.2f}" for k in QST_TRACED],
                        int(t.held_out), " | ".join(t.flags)])


def working_paper_md(treated: list[Treated], lines: dict[str, int], rules: Rules, business: dict,
                     extractor: str, due: date, effective_due: date) -> str:
    rl = rules.raw["return_lines"]
    out = [
        f"# GST/HST working paper: {business['name']}",
        "",
        f"- Registrant BN: {business['bn']} ({business['province']}, {business['method']} method, "
        f"{business['reporting_frequency']})",
        f"- Reporting period: {business['period_start']} to {business['period_end']}",
        f"- Classifier: `{extractor}`; rules: `04_gst_hst/rules.yaml` v{rules.raw['version']}",
        f"- Filing and payment due: {due.isoformat()}"
        + (f" (falls on a weekend or holiday; on time if received by {effective_due.isoformat()})"
           if effective_due != due else ""),
        f"- Transactions: {len(treated)}; flagged: {sum(1 for t in treated if t.flags)}; "
        f"held out of the return: {sum(1 for t in treated if t.held_out)}",
        "- Status: prepared for review. Not filed.",
        "",
        "## GST34 summary",
        "",
        "| Line | Description | Amount |",
        "|---|---|---:|",
    ]
    for k in LINE_ORDER:
        out.append(f"| {k} | {_esc(rl[k])} | {fmt(lines[k])} |")

    out += ["", "## Line traces", "",
            "Every transaction that feeds a line, so each total can be ticked back to the ledger export.", ""]
    for k in TRACED:
        rows = [t for t in treated if t.lines.get(k)]
        out += [f"### Line {k}: {_esc(rl[k])}", ""]
        if not rows:
            out += ["No transactions.", ""]
            continue
        out += ["| Txn | Date | Description | Category | Place | Rate | Net | GST/HST shown | Contribution |",
                "|---|---|---|---|---|---:|---:|---:|---:|"]
        for t in rows:
            x = t.txn
            out.append(f"| {x.txn_id} | {x.date} | {_esc(x.description)} | {t.cls.category} | {t.place} | "
                       f"{t.rate:.0%} | {fmt(x.net_cents)} | {fmt(x.tax_cents)} | {fmt(t.lines[k])} |")
        total = sum(t.lines[k] for t in rows)
        out += [f"| | | **Total ({len(rows)} transactions)** | | | | | | **{fmt(total)}** |", ""]

    off = [t for t in treated if not any(t.lines.get(k) for k in TRACED)]
    out += ["## Transactions on no line", "",
            "Exempt or out-of-scope items, ITC-ineligible purchases, and anything held out pending review.", "",
            "| Txn | Date | Description | Category | Supply type | ITC | Why no line |",
            "|---|---|---|---|---|---|---|"]
    for t in off:
        x = t.txn
        why = ("held out: " + t.flags[-1]) if t.held_out else (
            "no ITC" if t.itc_treatment == "none" and x.tax_cents else
            f"{t.supply_type}, no tax" if t.supply_type in ("exempt", "out_of_scope") else "no tax shown")
        out.append(f"| {x.txn_id} | {x.date} | {_esc(x.description)} | {t.cls.category} | {t.supply_type} | "
                   f"{t.itc_treatment} | {_esc(why)} |")

    flagged = [t for t in treated if t.flags]
    out += ["", f"## Flags for review ({len(flagged)})", "",
            "| Txn | Description | Flags |", "|---|---|---|"]
    for t in flagged:
        out.append(f"| {t.txn.txn_id} | {_esc(t.txn.description)} | {_esc('; '.join(t.flags))} |")
    out.append("")
    return "\n".join(out)


def qst_worksheet_md(treated: list[Treated], qst: dict[str, int], rules: Rules, business: dict,
                     due: date, effective_due: date) -> str:
    """The QST part of Revenu Quebec's return (FPZ-500), traced to transactions like the GST34."""
    q = rules.raw["qst"]
    rl = q["return_lines"]
    out = [
        f"# QST worksheet: {business['name']}",
        "",
        "Line-by-line equivalent of the QST part of Revenu Quebec's GST/HST and QST return (FPZ-500).",
        "Prepared for review. Not filed.",
        "",
        f"- QST registration number: {business.get('qst_number', 'not on file')}",
        f"- Reporting period: {business['period_start']} to {business['period_end']} (same period as the GST34)",
        f"- Rate: {float(q['rate']) * 100:.3f}% on the price excluding GST",
        f"- Due: {due.isoformat()}" + (f" (on time if received by {effective_due.isoformat()})"
                                      if effective_due != due else "")
        + ", assumed to follow the GST/HST due date for the same period; confirm with Revenu Quebec",
        f"- Sources: {q['sources']['itrs']['url']} ; {q['sources']['return_guide']['url']}",
        "",
        "| Line | Description | Amount |",
        "|---|---|---:|",
    ]
    for k in QST_LINE_ORDER:
        out.append(f"| {k} | {_esc(rl[k])} | {fmt(qst[k])} |")
    result = (f"QST owing to Revenu Quebec: {fmt(qst['209'])}" if qst["209"] > 0
              else f"QST refund: {fmt(-qst['209'])}" if qst["209"] < 0 else "Nil")
    out += ["", f"**{result}**", "", "## Line traces", ""]
    for k in QST_TRACED:
        rows = [t for t in treated if t.qst_lines.get(k)]
        out += [f"### Line {k}: {_esc(rl[k])}", ""]
        if not rows:
            out += ["No transactions.", ""]
            continue
        out += ["| Txn | Date | Description | Category | Net | QST shown | Supplier QST no. | Contribution |",
                "|---|---|---|---|---:|---:|---|---:|"]
        for t in rows:
            x = t.txn
            out.append(f"| {x.txn_id} | {x.date} | {_esc(x.description)} | {t.cls.category} | {fmt(x.net_cents)} | "
                       f"{fmt(x.qst_cents)} | {x.supplier_qst or '-'} | {fmt(t.qst_lines[k])} |")
        total = sum(t.qst_lines[k] for t in rows)
        out += [f"| | | **Total ({len(rows)} transactions)** | | | | | **{fmt(total)}** |", ""]
    no_itr = [t for t in treated if t.txn.qst_cents and not t.qst_lines]
    if no_itr:
        out += ["## QST shown but on no line", "", "| Txn | Description | Category | QST | Why |", "|---|---|---|---:|---|"]
        for t in no_itr:
            why = "held out: " + t.flags[-1] if t.held_out else "no ITR for this category"
            out.append(f"| {t.txn.txn_id} | {_esc(t.txn.description)} | {t.cls.category} | {fmt(t.txn.qst_cents)} | "
                       f"{_esc(why)} |")
        out.append("")
    out += ["## Not modelled", "", q["treatment"]["not_modelled"], ""]
    return "\n".join(out)


def filing_package_md(lines: dict[str, int], business: dict, due: date, effective_due: date,
                      treated: list[Treated], rules: Rules, qst: dict[str, int] | None = None) -> str:
    flagged = [t for t in treated if t.flags]
    out = [
        f"# GST/HST return package: {business['name']}, {business['period_start']} to {business['period_end']}",
        "",
        "Prepared by the gst_hst agent for review. **Nothing has been filed or paid.** A person enters the",
        "approved figures in GST/HST NETFILE or My Business Account, or gives them to the accountant.",
        "",
        f"- Business number: {business['bn']}",
        f"- Due date (return and payment): **{due.isoformat()}**"
        + (f", on time if received by {effective_due.isoformat()}" if effective_due != due else ""),
        f"- Rule: {rules.raw['filing']['rule']} {rules.raw['filing']['weekend_holiday']}",
        f"- Source: {rules.source_url('deadlines')}",
        "",
        "| Line | Amount |",
        "|---|---:|",
    ]
    for k in LINE_ORDER:
        out.append(f"| {k} | {fmt(lines[k])} |")
    result = (f"Amount owing (line 115): {fmt(lines['115'])}" if lines["115"]
              else f"Refund claimed (line 114): {fmt(lines['114'])}" if lines["114"] else "Nil return")
    out += ["", f"**{result}**", "", "## Before filing", "",
            f"- [ ] Resolve {len(flagged)} flagged transactions (see working_paper.md)",
            "- [ ] Confirm no instalments (line 110) or rebates (line 111) apply",
            "- [ ] Confirm no taxable real property purchases (line 205)",
            "- [ ] Keep supplier invoices supporting every ITC on line 106 (SOR/91-45 s. 3)",
            ""]
    if qst is not None:
        out += ["## QST (Revenu Quebec)", "",
                f"- QST registration number: {business.get('qst_number', 'not on file')}",
                "- Detail and traces: qst_worksheet.md", "",
                "| Line | Amount |", "|---|---:|"]
        out += [f"| {k} | {fmt(qst[k])} |" for k in QST_LINE_ORDER]
        out += ["", "- [ ] Keep supplier invoices showing the QST number for every ITR on line 206", ""]
    return "\n".join(out)


def write_all(out_dir: Path, treated: list[Treated], lines: dict[str, int], rules: Rules, business: dict,
              extractor: str, due: date, effective_due: date, qst: dict[str, int] | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "working_paper_md": out_dir / "working_paper.md",
        "working_paper_csv": out_dir / "working_paper.csv",
        "gst34_json": out_dir / "gst34.json",
        "package_md": out_dir / "filing_package.md",
    }
    paths["working_paper_md"].write_text(working_paper_md(treated, lines, rules, business, extractor, due, effective_due))
    write_csv(treated, paths["working_paper_csv"])
    paths["gst34_json"].write_text(json.dumps({
        "business_bn": business["bn"], "period_start": business["period_start"], "period_end": business["period_end"],
        "due_date": due.isoformat(), "effective_due_date": effective_due.isoformat(), "unit": "cents",
        "lines": lines}, indent=2) + "\n")
    paths["package_md"].write_text(filing_package_md(lines, business, due, effective_due, treated, rules, qst))
    if qst is not None:
        paths["qst_worksheet_md"] = out_dir / "qst_worksheet.md"
        paths["qst_json"] = out_dir / "qst.json"
        paths["qst_worksheet_md"].write_text(qst_worksheet_md(treated, qst, rules, business, due, effective_due))
        paths["qst_json"].write_text(json.dumps({
            "qst_number": business.get("qst_number"), "period_start": business["period_start"],
            "period_end": business["period_end"], "unit": "cents", "lines": qst}, indent=2) + "\n")
    return paths
