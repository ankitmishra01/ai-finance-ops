"""Year-end slips from the year's pay runs: T4 slips and T4 Summary, RL-1 for Quebec
employees, a SAMPLE T4 XML file and HTML/PDF summaries.

  uv run python 03_payroll/payroll_yearend.py                                # Ontario employer
  uv run python 03_payroll/payroll_yearend.py --data data/synthetic/payroll_qc  # Montreal employer

Every amount is summed from PayLine objects produced by payroll_history.replay_year, so the
slips reconcile to the pay runs by construction. Assumes every line of every run was
approved and paid (the current period's flagged lines are held in a real run).

Box rules (CRA RC4120, Rev. 25; RL-1 form 2024-10; Revenu Québec line 98.2 help page):
- T4 box 22 is federal tax only for a Quebec employee (Quebec tax goes on RL-1 box E).
- Box 24 EI insurable earnings up to the year's maximum ($68,900 for 2026).
- Box 26 CPP/QPP pensionable earnings up to the YAMPE ($85,000 for 2026).
- Box 56 PPIP insurable earnings up to $103,000 (2026 QPIP maximum).
- RL-1 B.A = QPP base + first additional; B.B = second additional (QPP2); G = QPP
  pensionable salary; H = QPIP premium; I = QPIP eligible salary.

The XML follows the element names and order in the CRA's 2026 T4 and T619 XML
specifications and the 2027 schema package (xmlschm1-27-1). It is marked SAMPLE in an XML
comment and in the submission reference; it is built from synthetic data and is not for filing.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from payroll import DATA, Company, load_inputs  # noqa: E402
from payroll_calc import D0, QC, RATES, money  # noqa: E402
from payroll_extract import default_extractor  # noqa: E402
from payroll_history import PeriodRun, replay_year  # noqa: E402
from shared.mail import read_mailbox  # noqa: E402

OUT = ROOT / "out" / "payroll_yearend"
SAMPLE_BANNER = "SAMPLE - synthetic data - NOT FOR FILING"


def _s(x: Decimal) -> str:
    return f"{x:.2f}"


def build_year(data_dir: Path = DATA, extractor_name: str | None = None) -> tuple:
    inputs = load_inputs(data_dir)
    roster = [e.name for e in inputs.employees]
    extractor = default_extractor(extractor_name)
    adjs = []
    for m in read_mailbox(data_dir / "inbox"):
        for a in extractor.extract(m, roster=roster, period_start=inputs.period.start):
            a["source"] = m.path.name
            adjs.append(a)
    return inputs, replay_year(inputs, adjs, data_dir)


def employee_totals(runs: list[PeriodRun]) -> dict[str, dict]:
    """Per-employee sums over every pay line of the year."""
    out: dict[str, dict] = {}
    keys = ["gross", "cpp", "cpp2", "ei", "qpip", "federal_tax", "provincial_tax", "employer_cpp",
            "employer_cpp2", "employer_ei", "employer_qpip", "employer_hsf", "cra_remittance", "rq_remittance"]
    for pr in runs:
        for l in pr.run.lines:
            d = l.deductions
            t = out.setdefault(l.employee.id, {"employee": l.employee, "province": d.province,
                                               **{k: D0 for k in keys}})
            t["gross"] += l.gross
            for k in keys[1:]:
                t[k] += getattr(d, k)
    return out


# ---------------------------------------------------------------- T4

def t4_slips(runs: list[PeriodRun]) -> list[dict]:
    slips = []
    for eid, t in employee_totals(runs).items():
        e, qc = t["employee"], t["province"] == "QC"
        rr = QC if qc else RATES
        plan = "qpp" if qc else "cpp"
        sin = "".join(ch for ch in e.sin if ch.isdigit())
        slip = {
            "employee_id": eid, "name": e.name, "sin": sin if len(sin) == 9 else "000000000",
            "box10_province": t["province"],
            "box14_employment_income": t["gross"],
            f"box{'17' if qc else '16'}_{plan}": t["cpp"],
            f"box{'17A' if qc else '16A'}_{plan}2": t["cpp2"],
            "box18_ei": t["ei"],
            # Quebec tax is not reported on the T4 (RC4120, box 22).
            "box22_income_tax": t["federal_tax"] if qc else t["federal_tax"] + t["provincial_tax"],
            "box24_ei_insurable": min(t["gross"], rr["ei"]["max_insurable_earnings"]),
            "box26_pensionable": min(t["gross"], rr[f"{plan}2"]["yampe"]),
            "box28_exempt": {"cpp_qpp": 0, "ei": 0, **({"ppip": 0} if qc else {})},
            "box45_dental": 1,
            "flags": [],
        }
        if qc:
            slip["box55_ppip"] = t["qpip"]
            slip["box56_ppip_insurable"] = min(t["gross"], QC["qpip"]["max_insurable_earnings"])
        if slip["sin"] == "000000000":
            slip["flags"].append("SIN missing: reported as 000000000; obtain the SIN before filing")
        slips.append(slip)
    return slips


def t4_summary(runs: list[PeriodRun], slips: list[dict], company: Company, year: int) -> dict:
    tot = employee_totals(runs)
    on = [t for t in tot.values() if t["province"] != "QC"]
    line = {
        "line14_employment_income": sum((s["box14_employment_income"] for s in slips), D0),
        "line16_employees_cpp": sum((t["cpp"] for t in on), D0),
        "line16A_employees_cpp2": sum((t["cpp2"] for t in on), D0),
        "line18_employees_ei": sum((s["box18_ei"] for s in slips), D0),
        # RC4120: "multiply the employees' total premiums by the employer's premium rate" (1.4 in
        # both Ontario and Quebec). Remittances used 1.4 x each period's premium, rounded per
        # period, so line 82 can differ from line 80 by a few cents; the CRA generally does not
        # charge or refund a difference of $2 or less.
        "line19_employer_ei": money(sum((s["box18_ei"] for s in slips), D0) * RATES["ei"]["employer_multiplier"]),
        "line22_income_tax": sum((s["box22_income_tax"] for s in slips), D0),
        "line27_employer_cpp": sum((t["employer_cpp"] for t in on), D0),
        "line27A_employer_cpp2": sum((t["employer_cpp2"] for t in on), D0),
    }
    line["line80_total_deductions"] = sum(line[k] for k in ["line16_employees_cpp", "line16A_employees_cpp2",
                                                             "line18_employees_ei", "line19_employer_ei",
                                                             "line22_income_tax", "line27_employer_cpp",
                                                             "line27A_employer_cpp2"])
    remitted = sum((t["cra_remittance"] for t in tot.values()), D0)
    line["line82_remittances"] = remitted
    diff = line["line80_total_deductions"] - remitted
    line["line84_overpayment"] = -diff if diff < 0 else D0
    line["line86_balance_due"] = diff if diff > 0 else D0
    line["line88_slips"] = len(slips)
    return {"label": f"T4 Summary {year} ({SAMPLE_BANNER})", "payroll_account": company.payroll_account,
            "employer": company.name, "year": year, **line}


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = str(text)
    return e


def _split_name(name: str) -> tuple[str, str, str]:
    parts = name.split()
    return parts[-1], parts[0], (parts[1][0] if len(parts) > 2 else "")


def _phone(p: str) -> tuple[str, str]:
    digits = "".join(ch for ch in p if ch.isdigit())
    return digits[:3], f"{digits[3:6]}-{digits[6:10]}"


def t4_xml(slips: list[dict], summary: dict, company: Company, year: int, employees: dict) -> str:
    """CRA T619 + T4 return. SAMPLE: built from synthetic data, not for filing."""
    sub = ET.Element("Submission", {"xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})
    t619 = _el(sub, "T619")
    _el(_el(t619, "TransmitterAccountNumber"), "bn15", company.payroll_account)
    _el(t619, "sbmt_ref_id", "SAMPLE01")
    _el(t619, "summ_cnt", 1)
    _el(t619, "lang_cd", "E")
    _el(_el(t619, "TransmitterName"), "l1_nm", company.name[:35])
    _el(t619, "TransmitterCountryCode", "CAN")
    area, num = _phone(company.contact_phone or "000-555-0100")
    c = _el(t619, "CNTC")
    _el(c, "cntc_nm", (company.contact_name or "Payroll Sample")[:35])
    _el(c, "cntc_area_cd", area)
    _el(c, "cntc_phn_nbr", num)
    _el(c, "cntc_email_area", company.contact_email or "payroll@example.test")

    t4 = _el(_el(sub, "Return"), "T4")
    for s in slips:
        e = employees[s["employee_id"]]
        qc = s["box10_province"] == "QC"
        slip = _el(t4, "T4Slip")
        nm = _el(slip, "EMPE_NM")
        snm, gvn, init = _split_name(e.name)
        _el(nm, "snm", snm[:20])
        _el(nm, "gvn_nm", gvn[:12])
        if init:
            _el(nm, "init", init)
        if e.address:
            ad = _el(slip, "EMPE_ADDR")
            _el(ad, "addr_l1_txt", e.address[:30])
            _el(ad, "cty_nm", e.city[:28])
            _el(ad, "prov_cd", s["box10_province"])
            _el(ad, "cntry_cd", "CAN")
            _el(ad, "pstl_cd", e.postal_code.replace(" ", "").upper())
        _el(slip, "sin", s["sin"])
        _el(slip, "empe_nbr", s["employee_id"])
        _el(slip, "bn", company.payroll_account)
        _el(slip, "cpp_qpp_xmpt_cd", 0)
        _el(slip, "ei_xmpt_cd", 0)
        if qc:
            _el(slip, "prov_pip_xmpt_cd", 0)
        _el(slip, "rpt_tcd", "O")
        _el(slip, "empt_prov_cd", s["box10_province"])
        _el(slip, "empr_dntl_ben_rpt_cd", s["box45_dental"])
        amt = _el(slip, "T4_AMT")
        _el(amt, "empt_incamt", _s(s["box14_employment_income"]))
        if qc:
            if s["box17_qpp"]:
                _el(amt, "qpp_cntrb_amt", _s(s["box17_qpp"]))
            if s["box17A_qpp2"]:
                _el(amt, "qppe_cntrb_amt", _s(s["box17A_qpp2"]))
        else:
            if s["box16_cpp"]:
                _el(amt, "cpp_cntrb_amt", _s(s["box16_cpp"]))
            if s["box16A_cpp2"]:
                _el(amt, "cppe_cntrb_amt", _s(s["box16A_cpp2"]))
        if s["box18_ei"]:
            _el(amt, "empe_eip_amt", _s(s["box18_ei"]))
        _el(amt, "itx_ddct_amt", _s(s["box22_income_tax"]))
        _el(amt, "ei_insu_ern_amt", _s(s["box24_ei_insurable"]))
        _el(amt, "cpp_qpp_ern_amt", _s(s["box26_pensionable"]))
        if qc:
            _el(amt, "prov_pip_amt", _s(s["box55_ppip"]))
            _el(amt, "prov_insu_ern_amt", _s(s["box56_ppip_insurable"]))

    sm = _el(t4, "T4Summary")
    _el(sm, "bn", company.payroll_account)
    _el(_el(sm, "EMPR_NM"), "l1_nm", company.name[:30])
    if company.street:
        ad = _el(sm, "EMPR_ADDR")
        _el(ad, "addr_l1_txt", company.street[:30])
        _el(ad, "cty_nm", company.city[:28])
        _el(ad, "prov_cd", company.province)
        _el(ad, "cntry_cd", "CAN")
        _el(ad, "pstl_cd", company.postal_code.replace(" ", "").upper())
    c = _el(sm, "CNTC")
    _el(c, "cntc_nm", (company.contact_name or "Payroll Sample")[:22])
    _el(c, "cntc_area_cd", area)
    _el(c, "cntc_phn_nbr", num)
    _el(sm, "tx_yr", year)
    _el(sm, "slp_cnt", len(slips))
    _el(sm, "rpt_tcd", "O")
    ta = _el(sm, "T4_TAMT")
    _el(ta, "tot_empt_incamt", _s(summary["line14_employment_income"]))
    _el(ta, "tot_empe_cpp_amt", _s(summary["line16_employees_cpp"]))
    _el(ta, "tot_empe_cppe_amt", _s(summary["line16A_employees_cpp2"]))
    _el(ta, "tot_empe_eip_amt", _s(summary["line18_employees_ei"]))
    _el(ta, "tot_itx_ddct_amt", _s(summary["line22_income_tax"]))
    _el(ta, "tot_empr_cpp_amt", _s(summary["line27_employer_cpp"]))
    _el(ta, "tot_empr_cppe_amt", _s(summary["line27A_employer_cpp2"]))
    _el(ta, "tot_empr_eip_amt", _s(summary["line19_employer_ei"]))

    ET.indent(sub)
    body = ET.tostring(sub, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<!-- {SAMPLE_BANNER}. Generated from synthetic pay runs by ai-finance-ops 03_payroll. "
            "Element names and order follow the CRA T619 and T4 XML specifications (2026 versions) "
            "and schema package xmlschm1-27-1. Account numbers and SINs are fictitious. -->\n" + body + "\n")


# ---------------------------------------------------------------- RL-1

def rl1_slips(runs: list[PeriodRun]) -> list[dict]:
    out = []
    for eid, t in employee_totals(runs).items():
        if t["province"] != "QC":
            continue
        e = t["employee"]
        out.append({
            "employee_id": eid, "name": e.name, "sin": "".join(ch for ch in e.sin if ch.isdigit()),
            "A_employment_income": t["gross"],
            "B.A_qpp_contribution": t["cpp"],
            "B.B_qpp_second_additional": t["cpp2"],
            "C_ei_premium": t["ei"],
            "E_quebec_income_tax": t["provincial_tax"],
            # G: up to the YAMPE when B.B has an amount, otherwise up to the MPE.
            "G_qpp_pensionable_salary": min(t["gross"], QC["qpp2"]["yampe"] if t["cpp2"] else QC["qpp"]["ympe"]),
            "H_qpip_premium": t["qpip"],
            "I_qpip_eligible_salary": min(t["gross"], QC["qpip"]["max_insurable_earnings"]),
        })
    return out


def rl1_totals(runs: list[PeriodRun], slips: list[dict]) -> dict | None:
    if not slips:
        return None
    tot = [t for t in employee_totals(runs).values() if t["province"] == "QC"]
    s = lambda k: sum((x[k] for x in slips), D0)  # noqa: E731
    out = {
        "label": f"RL-1 totals ({SAMPLE_BANNER}; not the RLZ-1.S form)",
        "slips": len(slips),
        "A_employment_income": s("A_employment_income"),
        "qpp_employee": s("B.A_qpp_contribution") + s("B.B_qpp_second_additional"),
        "qpp_employer": sum((t["employer_cpp"] + t["employer_cpp2"] for t in tot), D0),
        "qpip_employee": s("H_qpip_premium"),
        "qpip_employer": sum((t["employer_qpip"] for t in tot), D0),
        "quebec_income_tax": s("E_quebec_income_tax"),
        "health_services_fund": sum((t["employer_hsf"] for t in tot), D0),
    }
    out["total_due_to_revenu_quebec"] = sum((v for k, v in out.items() if k not in {"label", "slips", "A_employment_income"}), D0)
    out["remitted_in_year"] = sum((t["rq_remittance"] for t in tot), D0)
    return out


# ---------------------------------------------------------------- outputs

def _jsonable(x):
    if isinstance(x, Decimal):
        return str(x.quantize(Decimal("0.01")))
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_jsonable(v) for v in x]
    return x


def _table(rows: list[dict], cols: list[str]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    def fmt(v):
        return f"{v:,.2f}" if isinstance(v, Decimal) else str(v)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(fmt(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def summary_html(company: Company, year: int, slips, summary, rl1, rl1_tot, projected: list[int], roe=None) -> str:
    t4_cols = [k for k in slips[0] if k not in {"box28_exempt", "flags"}] if slips else []
    parts = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>Year-end {year} sample</title><style>"
        ":root{--fg:#1c1c1c;--bg:#fff;--line:#ccc;--warn:#9a3412}"
        "@media (prefers-color-scheme: dark){:root{--fg:#eee;--bg:#161616;--line:#444;--warn:#fb923c}}"
        "body{font:14px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:16px}"
        "table{border-collapse:collapse;margin:8px 0 20px;display:block;overflow-x:auto}"
        "td,th{border:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}"
        "th{text-align:left}.banner{color:var(--warn);font-weight:700}</style></head><body>",
        f"<p class='banner'>{SAMPLE_BANNER}</p>",
        f"<h1>{html.escape(company.name)}: {year} year-end</h1>",
        f"<p>Built from {24} replayed pay runs. Periods {', '.join(map(str, projected)) or 'none'} are projections "
        "(no changes after the current period). Every line is assumed approved and paid.</p>",
        "<h2>T4 Summary</h2>",
        _table([{"line": k, "amount": v} for k, v in summary.items() if k.startswith("line")], ["line", "amount"]),
        "<h2>T4 slips</h2>", _table(slips, t4_cols),
    ]
    if rl1:
        parts += ["<h2>RL-1 slips (Quebec employees)</h2>", _table(rl1, list(rl1[0].keys())),
                  "<h2>RL-1 totals and Revenu Québec reconciliation</h2>",
                  _table([{"item": k, "amount": v} for k, v in rl1_tot.items() if k != "label"], ["item", "amount"])]
    if roe:
        parts += [f"<h2>Record of Employment data: {html.escape(roe['block9_employee_name'])}</h2>",
                  f"<p>Reason {roe['block16_reason_code']} ({html.escape(roe['block16_reason'])}); "
                  f"15A insurable hours {roe['block15A_total_insurable_hours']}; "
                  f"15B insurable earnings {roe['block15B_total_insurable_earnings']}.</p>",
                  _table(roe["block15C_insurable_earnings_by_pay_period"],
                         ["pay_period", "period_end", "insurable_earnings", "insurable_hours"])]
    parts.append("</body></html>\n")
    return "".join(parts)


def summary_pdf(path: Path, company: Company, year: int, slips, summary, rl1, rl1_tot) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    style = TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("FONTSIZE", (0, 0), (-1, -1), 7), ("ALIGN", (1, 1), (-1, -1), "RIGHT")])
    doc = SimpleDocTemplate(str(path), pagesize=landscape(LETTER), title=f"Year-end {year} sample")
    story = [Paragraph(f"{company.name}: {year} year-end", st["Title"]),
             Paragraph(SAMPLE_BANNER, st["Italic"]), Spacer(1, 8),
             Paragraph("T4 Summary", st["Heading2"]),
             Table([["Line", "Amount"]] + [[k, str(v)] for k, v in summary.items() if k.startswith("line")], style=style),
             Paragraph("T4 slips", st["Heading2"])]
    cols = ["employee_id", "name", "box10_province", "box14_employment_income", "box18_ei", "box22_income_tax",
            "box24_ei_insurable", "box26_pensionable"]
    story.append(Table([cols] + [[str(s.get(c, "")) for c in cols] for s in slips], style=style))
    if rl1:
        story.append(Paragraph("RL-1 slips", st["Heading2"]))
        rc = list(rl1[0].keys())
        story.append(Table([[c.split("_")[0] for c in rc]] + [[str(r[c]) for c in rc] for r in rl1], style=style))
        story.append(Paragraph("RL-1 totals", st["Heading2"]))
        story.append(Table([["Item", "Amount"]] + [[k, str(v)] for k, v in rl1_tot.items() if k != "label"], style=style))
    doc.build(story)
    return path


def main(argv: list[str] | None = None) -> int:
    from payroll_roe import roe_for, write_roe

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--extractor", choices=["rules", "claude"], default=None)
    args = ap.parse_args(argv)
    out = args.out or (OUT / args.data.name)
    out.mkdir(parents=True, exist_ok=True)

    inputs, runs = build_year(args.data, args.extractor)
    co, year = inputs.company, inputs.period.pay_date.year
    employees = {e.id: e for e in inputs.employees}
    projected = [pr.period.number for pr in runs if pr.kind == "projected"]

    slips = t4_slips(runs)
    summ = t4_summary(runs, slips, co, year)
    rl1 = rl1_slips(runs)
    rl1_tot = rl1_totals(runs, rl1)
    roes = [roe_for(e, runs, co) for e in inputs.employees if e.end_date]
    for r in roes:
        write_roe(r, out)

    (out / "t4_slips.json").write_text(json.dumps(_jsonable(slips), indent=2, ensure_ascii=False) + "\n")
    (out / "t4_summary.json").write_text(json.dumps(_jsonable(summ), indent=2, ensure_ascii=False) + "\n")
    (out / "SAMPLE_NOT_FOR_FILING_t4.xml").write_text(t4_xml(slips, summ, co, year, employees))
    if rl1:
        (out / "rl1_slips.json").write_text(json.dumps(_jsonable(rl1), indent=2, ensure_ascii=False) + "\n")
        (out / "rl1_totals.json").write_text(json.dumps(_jsonable(rl1_tot), indent=2, ensure_ascii=False) + "\n")
    (out / "yearend_summary.html").write_text(summary_html(co, year, slips, summ, rl1, rl1_tot, projected,
                                                           roes[0] if roes else None))
    summary_pdf(out / "yearend_summary.pdf", co, year, slips, summ, rl1, rl1_tot)

    rel = os.path.relpath(out)
    print(f"Year-end {year} for {co.name} ({SAMPLE_BANNER})")
    print(f"{len(runs)} pay runs replayed; projected periods: {projected or 'none'}")
    print(f"T4 slips: {len(slips)}  line 14 {summ['line14_employment_income']:,.2f}  "
          f"line 80 {summ['line80_total_deductions']:,.2f}  line 82 {summ['line82_remittances']:,.2f}  "
          f"difference {summ['line80_total_deductions'] - summ['line82_remittances']:,.2f}")
    if rl1:
        print(f"RL-1 slips: {len(rl1)}  box A {rl1_tot['A_employment_income']:,.2f}  "
              f"due to Revenu Québec {rl1_tot['total_due_to_revenu_quebec']:,.2f}  "
              f"remitted {rl1_tot['remitted_in_year']:,.2f}")
    for r in roes:
        print(f"ROE: {r['block9_employee_name']} reason {r['block16_reason_code']}, "
              f"15A {r['block15A_total_insurable_hours']} h, 15B {r['block15B_total_insurable_earnings']}")
    for s in slips:
        for f in s["flags"]:
            print(f"FLAG {s['employee_id']}: {f}")
    print(f"Outputs in {rel}/ (T4 XML: SAMPLE_NOT_FOR_FILING_t4.xml)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
