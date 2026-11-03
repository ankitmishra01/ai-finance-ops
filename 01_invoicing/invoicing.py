"""Deterministic core of month-end invoicing.

Everything here is plain code: which clients are billable, invoice lines, GST/HST by place
of supply, QST/PST/RST where the supplier is registered for it, due dates, CRA invoice-content
checks and anomaly flags. The only model step
(reading contracts and emails for customer details) is injected as an Extractor.
Amounts are integer cents; hours x rate is rounded half-up to the cent.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from email.utils import parseaddr
from pathlib import Path

import yaml

from shared import ledger, tax
from shared.approvals import ApprovalQueue
from shared.mail import Mail, read_mailbox

from invoicing_extract import Email, ExtractionInput, Extractor

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "invoicing"
SUPPLIER_CONFIG = Path(__file__).resolve().parent / "supplier_config.yaml"

# CRA: information required on an invoice, by total amount (tax included), to support the
# recipient's input tax credit claim. Input Tax Credit Information (GST/HST) Regulations
# (SOR/91-45) s.3; summarised at
# https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/calculate-prepare-report/input-tax-credit.html
CRA_FIELDS_BY_TIER = {
    "under $100": ["supplier_name", "issue_date", "total"],
    "$100 to $499.99": ["supplier_name", "issue_date", "total", "supplier_gst_hst_number", "tax_statement"],
    "$500 or more": ["supplier_name", "issue_date", "total", "supplier_gst_hst_number", "tax_statement",
                     "customer_name", "line_descriptions", "payment_terms"],
}


# ---------------------------------------------------------------------------
# Maths and dates
# ---------------------------------------------------------------------------

def line_amount_cents(hours, rate_cents: int) -> int:
    """hours x rate, rounded half-up to the cent. Hours may be float or str (e.g. 1.25)."""
    amt = Decimal(str(hours)) * Decimal(rate_cents)
    return int(amt.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def tax_for(subtotal_cents: int, province: str) -> tuple[int, str, str]:
    """GST/HST by place of supply. Returns (tax cents, tax code, label like 'HST 13%')."""
    if province not in tax.RATES:
        raise ValueError(f"unknown province {province!r}")
    rate = tax.RATES[province][0]
    kind = "HST" if rate > 0.05 else "GST"
    pct = f"{rate * 100:g}%"
    return tax.gst_hst_cents(subtotal_cents, province), f"{kind}-{province}", f"{kind} {pct}"


def provincial_tax_for(subtotal_cents: int, province: str | None, registrations: dict) -> tuple[int, str, str, str]:
    """QST/PST/RST, charged only if the supplier is registered in the customer's province.

    Returns (tax cents, code like 'QST', label like 'QST 9.975%', supplier's registration number);
    (0, '', '', '') when nothing is charged. Computed on the subtotal before GST.
    """
    code = tax.provincial_tax_code(province)
    reg = registrations.get(province or "", {})
    if not code or not reg.get("registered"):
        return 0, "", "", ""
    return (tax.provincial_tax_cents(subtotal_cents, province), code, tax.provincial_tax_label(province),
            str(reg.get("number") or ""))


def due_date(issue: date, terms: str) -> date:
    """Supported: 'Due on receipt', 'Net N', 'Net N EOM' (N days after month end), '2/10 Net 30'."""
    t = re.sub(r"\s+", " ", terms.strip().lower())
    if t in {"due on receipt", "on receipt", "net 0", "upon receipt"}:
        return issue
    m = re.fullmatch(r"(?:\d+(?:\.\d+)?/\d+ )?net (\d+)( eom)?", t)
    if not m:
        raise ValueError(f"unrecognised payment terms {terms!r}")
    days = int(m.group(1))
    if m.group(2):
        base = issue.replace(day=calendar.monthrange(issue.year, issue.month)[1])
        return base + timedelta(days=days)
    return issue + timedelta(days=days)


def period_bounds(period: str) -> tuple[date, date]:
    y, mth = map(int, period.split("-"))
    return date(y, mth, 1), date(y, mth, calendar.monthrange(y, mth)[1])


def cra_tier(total_cents: int) -> str:
    if total_cents < 10000:
        return "under $100"
    if total_cents < 50000:
        return "$100 to $499.99"
    return "$500 or more"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Line:
    description: str
    quantity: str
    unit_price_cents: int
    amount_cents: int
    kind: str
    contract_id: str


@dataclass
class Customer:
    client_id: str
    legal_name: str | None
    billing_email: str | None
    province: str | None
    address: str | None
    po_number: str | None
    is_new: bool = False
    changes: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class DraftInvoice:
    number: str
    client_id: str
    contract_ids: list[str]
    period: str
    issue_date: str
    due_date: str
    payment_terms: str
    customer: dict
    place_of_supply: str
    lines: list[Line]
    subtotal_cents: int
    tax_cents: int
    tax_code: str
    tax_label: str
    total_cents: int
    po_number: str | None
    cra_tier: str
    flags: list[str] = field(default_factory=list)
    # QST/PST/RST, kept apart from GST/HST because it goes to a different authority.
    provincial_tax_cents: int = 0
    provincial_tax_code: str = ""
    provincial_tax_label: str = ""
    provincial_tax_number: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Plan:
    period: str
    issue_date: str
    supplier: dict
    customers: list[Customer]
    invoices: list[DraftInvoice]
    not_billed: list[dict]
    extractor: str


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_supplier_config(path: Path = SUPPLIER_CONFIG) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def load_ops(path: Path = DATA / "ops.json", supplier_config: Path | None = SUPPLIER_CONFIG) -> dict:
    """The ERP export, with the supplier's provincial registrations from supplier_config.yaml.
    supplier_config=None leaves them out, so only GST/HST is charged."""
    ops = json.loads(Path(path).read_text())
    if supplier_config is not None:
        regs = load_supplier_config(supplier_config).get("provincial_registrations", {})
        ops["supplier"].setdefault("provincial_registrations", regs)
    return ops


def mails_by_client(ops: dict, mails: list[Mail]) -> dict[str, list[Email]]:
    """Link each email to a client by the sender's domain."""
    by_domain = {c["domain"].lower(): c["client_id"] for c in ops["clients"]}
    out: dict[str, list[Email]] = {}
    for m in mails:
        domain = parseaddr(m.sender)[1].rpartition("@")[2].lower()
        cid = by_domain.get(domain)
        if cid:
            out.setdefault(cid, []).append(Email(m.sender, m.subject, m.date, m.body))
    return out


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

def contract_active(ct: dict, period: str) -> bool:
    start, end = period_bounds(period)
    c_start = date.fromisoformat(ct["start"]) if ct.get("start") else date.min
    c_end = date.fromisoformat(ct["end"]) if ct.get("end") else date.max
    return c_start <= end and c_end >= start


def contract_lines(ct: dict, ops: dict, period: str) -> tuple[list[Line], list[str], dict]:
    """Lines for one contract in a period, plus flags and activity stats."""
    start, end = period_bounds(period)
    in_period = lambda d: d and start <= date.fromisoformat(d) <= end  # noqa: E731
    entries = [t for t in ops["timesheets"] if t["contract_id"] == ct["contract_id"] and in_period(t["date"])]
    flags: list[str] = []
    lines: list[Line] = []
    cid = ct["contract_id"]
    if ct.get("currency", "CAD") != "CAD":
        flags.append(f"{cid}: contract currency {ct['currency']} is not CAD; not supported")

    if ct["type"] == "retainer":
        lines.append(Line(f"{ct['description']}, {period}", "1", ct["rate_cents"], ct["rate_cents"],
                          "retainer", cid))
        if not entries:
            flags.append(f"{cid}: active retainer but no logged activity in {period}; confirm it should be billed")
    elif ct["type"] == "hourly":
        off_rate = [t for t in entries if t["rate_cents"] != ct["rate_cents"]]
        if off_rate:
            ids = ", ".join(t["entry_id"] for t in off_rate)
            rates = ", ".join(sorted({tax.fmt(t["rate_cents"]) for t in off_rate}))
            flags.append(f"{cid}: timesheet rate {rates} differs from contract rate {tax.fmt(ct['rate_cents'])}"
                         f" on {ids}; billed at contract rate")
        by_role: dict[str, Decimal] = {}
        for t in entries:
            by_role[t["staff_role"]] = by_role.get(t["staff_role"], Decimal(0)) + Decimal(str(t["hours"]))
        for role, hrs in sorted(by_role.items()):
            lines.append(Line(f"{ct['description']}: {role}, {period}", f"{hrs.normalize():f} h",
                              ct["rate_cents"], line_amount_cents(hrs, ct["rate_cents"]), "hourly", cid))
        cap = ct.get("cap_cents")
        sub = sum(ln.amount_cents for ln in lines)
        if cap and sub > cap:
            flags.append(f"{cid}: hourly total {tax.fmt(sub)} exceeds the contract cap {tax.fmt(cap)}")
    elif ct["type"] == "milestone":
        for m in ops["milestones"]:
            if m["contract_id"] == cid and in_period(m.get("completed")):
                lines.append(Line(f"Milestone completed {m['completed']}: {m['name']}", "1",
                                  m["amount_cents"], m["amount_cents"], "milestone", cid))
    else:
        flags.append(f"{cid}: unknown contract type {ct['type']!r}")
    return lines, flags, {"entries": len(entries)}


def resolve_customer(client: dict, ops: dict, emails: list[Email], extractor: Extractor) -> Customer:
    """Existing record + changes from email, or a new record extracted from contract + email."""
    cid = client["client_id"]
    record = next((c for c in ops["customers"] if c["client_id"] == cid), None)
    contracts = [ct for ct in ops["contracts"] if ct["client_id"] == cid]
    contract_text = next((ct["contract_text"] for ct in contracts if ct.get("contract_text")), None)
    sup = ops["supplier"]["name"]

    if record:
        cust = Customer(cid, record["legal_name"], record["billing_email"], record["province"],
                        record["address"], record.get("po_number"))
        if emails:
            got = extractor.extract(ExtractionInput(client["name"], sup, None, emails))
            for f in ("legal_name", "billing_email", "province", "address", "po_number"):
                v = got.get(f)
                if v and v != getattr(cust, f):
                    cust.changes[f] = {"from": getattr(cust, f), "to": v}
                    setattr(cust, f, v)
            if got.get("split_invoices"):
                cust.changes["split_invoices"] = {"from": False, "to": True}
            cust.evidence = got.get("evidence", {})
        return cust

    got = extractor.extract(ExtractionInput(client["name"], sup, contract_text, emails))
    cust = Customer(cid, got.get("legal_name"), got.get("billing_email"), got.get("province"),
                    got.get("address"), got.get("po_number"), is_new=True, evidence=got.get("evidence", {}))
    if got.get("split_invoices"):
        cust.changes["split_invoices"] = {"from": False, "to": True}
    if not cust.legal_name:
        cust.legal_name = client["name"]
        cust.flags.append("legal name not found in contract or email; using the ERP name")
    contract_prov = next((ct["billing_province"] for ct in contracts if ct.get("billing_province")), None)
    if not cust.province:
        cust.province = contract_prov
        cust.flags.append(f"bill-to province not found; using contract billing province {contract_prov}")
    elif contract_prov and cust.province != contract_prov:
        cust.flags.append(f"bill-to province {cust.province} differs from contract province {contract_prov};"
                          " confirm place of supply")
    return cust


def customer_flags(cust: Customer, po_required: bool) -> list[str]:
    flags = list(cust.flags)
    if not cust.billing_email:
        flags.append("missing billing email")
    if po_required and not cust.po_number:
        flags.append("PO required by contract but none on file or in email")
    if cust.province not in tax.RATES:
        flags.append(f"place of supply unknown ({cust.province}); cannot compute GST/HST")
    return flags


def check_cra_fields(inv: DraftInvoice, supplier: dict) -> list[str]:
    """Names of CRA-required invoice fields that are missing for this invoice's tier."""
    present = {
        "supplier_name": bool(supplier.get("name")),
        "issue_date": bool(inv.issue_date),
        "total": True,
        "supplier_gst_hst_number": bool(re.fullmatch(r"\d{9} ?RT ?\d{4}", supplier.get("gst_hst_number") or "")),
        "tax_statement": bool(inv.tax_label),
        "customer_name": bool(inv.customer.get("legal_name")),
        "line_descriptions": all(ln.description for ln in inv.lines),
        "payment_terms": bool(inv.payment_terms),
    }
    return [f for f in CRA_FIELDS_BY_TIER[inv.cra_tier] if not present[f]]


def provincial_tax_flags(inv: DraftInvoice) -> list[str]:
    """Checks on the QST/PST/RST charged. Each one holds the invoice for a person."""
    if not inv.provincial_tax_cents:
        return []
    flags = []
    if inv.provincial_tax_code == "QST":
        # Revenu Quebec ties a customer's input tax refund to the supplier's QST number on the
        # invoice, as the CRA does for ITCs; the $100 threshold below mirrors SOR/91-45 s. 3 (assumed).
        ok, why = tax.validate_qst_number(inv.provincial_tax_number)
        if not ok and inv.total_cents >= 10000:
            flags.append(f"QST charged but the supplier's QST number is {why}; set it in supplier_config.yaml")
    else:
        flags.append(f"{inv.provincial_tax_code} charged in {inv.place_of_supply}: {inv.place_of_supply} "
                     f"{inv.provincial_tax_code} applies only to listed services; confirm this supply is taxable")
        if not inv.provincial_tax_number:
            flags.append(f"{inv.provincial_tax_code} charged but no {inv.place_of_supply} registration number on file")
    return flags


def plan_month(ops: dict, mails: list[Mail], period: str, issue: date, extractor: Extractor) -> Plan:
    """Build customer and invoice proposals for one period. Pure: writes nothing."""
    supplier = ops["supplier"]
    registrations = supplier.get("provincial_registrations", {})
    linked = mails_by_client(ops, mails)
    issued = {(i["client_id"], i["period"]): i["number"] for i in ops.get("issued_invoices", [])}
    seq = supplier.get("next_invoice_seq", 1)
    customers: list[Customer] = []
    invoices: list[DraftInvoice] = []
    not_billed: list[dict] = []

    for client in sorted(ops["clients"], key=lambda c: c["client_id"]):
        cid = client["client_id"]
        if client.get("status") != "active":
            continue
        active = [ct for ct in ops["contracts"] if ct["client_id"] == cid and contract_active(ct, period)]
        if not active:
            not_billed.append({"client_id": cid, "reason": "no active contract in period"})
            continue

        groups: list[tuple[dict, list[Line], list[str]]] = []
        for ct in active:
            lines, flags, _ = contract_lines(ct, ops, period)
            if lines or flags:
                groups.append((ct, lines, flags))
        if not any(lines for _, lines, _ in groups):
            not_billed.append({"client_id": cid, "reason": "no billable activity in period"})
            continue

        cust = resolve_customer(client, ops, linked.get(cid, []), extractor)
        customers.append(cust)
        po_required = any(ct.get("po_required") for ct in active)
        c_flags = customer_flags(cust, po_required)

        split = "split_invoices" in cust.changes
        billable = [g for g in groups if g[1]]
        if split and len(billable) < 2:
            c_flags.append("client asked to split the invoice but has only one billable contract")
        cust.flags = c_flags
        bundles = [[g] for g in billable] if split and len(billable) > 1 else [billable]

        for bundle in bundles:
            lines = [ln for _, ls, _ in bundle for ln in ls]
            flags = [f for _, _, fs in bundle for f in fs] + c_flags
            terms = bundle[0][0]["payment_terms"]
            if len({g[0]["payment_terms"] for g in bundle}) > 1:
                flags.append("contracts on this invoice have different payment terms; using "
                             f"{terms!r}")
            subtotal = sum(ln.amount_cents for ln in lines)
            prov = cust.province if cust.province in tax.RATES else None
            tax_cents, code, label = tax_for(subtotal, prov) if prov else (0, "UNKNOWN", "")
            p_cents, p_code, p_label, p_number = provincial_tax_for(subtotal, prov, registrations)
            total = subtotal + tax_cents + p_cents
            if (cid, period) in issued:
                flags.append(f"duplicate: {issued[(cid, period)]} already issued to this client for {period}")
            inv = DraftInvoice(
                number=f"INV-{seq:05d}", client_id=cid, contract_ids=[g[0]["contract_id"] for g in bundle],
                period=period, issue_date=issue.isoformat(), due_date=due_date(issue, terms).isoformat(),
                payment_terms=terms, customer=asdict(cust), place_of_supply=prov or "",
                lines=lines, subtotal_cents=subtotal, tax_cents=tax_cents, tax_code=code, tax_label=label,
                total_cents=total, po_number=cust.po_number, cra_tier=cra_tier(total), flags=flags,
                provincial_tax_cents=p_cents, provincial_tax_code=p_code, provincial_tax_label=p_label,
                provincial_tax_number=p_number,
            )
            missing = check_cra_fields(inv, supplier)
            if missing:
                inv.flags.append(f"CRA invoice fields missing for {inv.cra_tier} tier: {', '.join(missing)}")
            inv.flags += provincial_tax_flags(inv)
            invoices.append(inv)
            seq += 1

    # Within-run duplicates (same client, same contract set) would be a bug upstream; guard anyway.
    seen: dict[tuple, str] = {}
    for inv in invoices:
        key = (inv.client_id, tuple(inv.contract_ids))
        if key in seen:
            inv.flags.append(f"duplicate of {seen[key]} in this run")
        seen[key] = inv.number

    return Plan(period, issue.isoformat(), supplier, customers, invoices, not_billed, extractor.name)


def load_mail(folder: Path = DATA / "mailbox") -> list[Mail]:
    return read_mailbox(folder)


# ---------------------------------------------------------------------------
# Approval and commit
# ---------------------------------------------------------------------------

RECORD_FIELDS = ("legal_name", "billing_email", "province", "address", "po_number")


def next_seq_from_ledger(con) -> int:
    """One past the highest INV-nnnnn number in the ledger (1 if none)."""
    nums = [int(m.group(1)) for (n,) in con.execute("select number from invoices")
            if (m := re.fullmatch(r"INV-(\d+)", n or ""))]
    return max(nums, default=0) + 1


def flag_ledger_duplicates(plan: Plan, con) -> None:
    """Flag invoices whose customer already has an invoice for the period in the ledger."""
    rows = con.execute(
        "select c.name, i.period, i.number from invoices i join customers c on c.id = i.customer_id"
    ).fetchall()
    have = {(r["name"], r["period"]): r["number"] for r in rows}
    for inv in plan.invoices:
        hit = have.get((inv.customer.get("legal_name"), inv.period))
        if hit:
            inv.flags.append(f"duplicate: ledger already has {hit} for this customer for {inv.period}")


def build_queue(plan: Plan) -> tuple[ApprovalQueue, dict[str, list[int]]]:
    """Propose customers first, then invoices. Returns the queue and, per invoice number,
    the indices of the customer proposals it depends on."""
    q = ApprovalQueue()
    cust_idx: dict[str, int] = {}
    for cust in plan.customers:
        record_changes = {k: v for k, v in cust.changes.items() if k in RECORD_FIELDS}
        payload = asdict(cust)
        if cust.is_new:
            q.propose("customer", f"NEW customer {cust.legal_name} ({cust.province}, "
                      f"{cust.billing_email or 'no email'}, PO {cust.po_number or '-'})", payload, cust.flags)
            cust_idx[cust.client_id] = len(q.items) - 1
        elif record_changes:
            txt = "; ".join(f"{k}: {v['from']} -> {v['to']}" for k, v in record_changes.items())
            q.propose("customer_update", f"UPDATE {cust.legal_name}: {txt}", payload, cust.flags)
            cust_idx[cust.client_id] = len(q.items) - 1
    deps: dict[str, list[int]] = {}
    for inv in plan.invoices:
        prov_part = (f" + {inv.provincial_tax_label} {tax.fmt(inv.provincial_tax_cents)}"
                     if inv.provincial_tax_cents else "")
        summary = (f"{inv.number} {inv.customer.get('legal_name')} {tax.fmt(inv.subtotal_cents)} + "
                   f"{inv.tax_label or 'tax ?'} {tax.fmt(inv.tax_cents)}{prov_part} = {tax.fmt(inv.total_cents)}, "
                   f"due {inv.due_date}")
        q.propose("invoice", summary, inv.to_dict(), inv.flags)
        deps[inv.number] = [cust_idx[inv.client_id]] if inv.client_id in cust_idx else []
    return q, deps


def enforce_dependencies(q: ApprovalQueue, deps: dict[str, list[int]]) -> None:
    """An invoice cannot be issued to a customer record that was not approved."""
    for p in q.items:
        if p.kind == "invoice" and p.decision == "approved":
            blocked = [i for i in deps.get(p.payload["number"], []) if q.items[i].decision != "approved"]
            if blocked:
                p.decision = "held"
                p.flags = p.flags + ["customer record change not approved; invoice held"]


def customer_record_fields(c: dict) -> dict:
    """Map an approved customer payload onto ledger.customers columns, skipping blanks."""
    cols = {"email": c.get("billing_email"), "province": c.get("province"), "address": c.get("address")}
    return {k: v for k, v in cols.items() if v}


def commit(q: ApprovalQueue, plan: Plan, con, out_dir: Path) -> dict:
    """Write approved customers and invoices to the ledger and render invoice PDFs."""
    from invoicing_pdf import render_invoice

    written = {"customers": 0, "invoices": 0, "pdfs": []}
    for p in q.items:
        if p.decision != "approved" or p.kind not in {"customer", "customer_update"}:
            continue
        c = p.payload
        cid = ledger.upsert_customer(con, c["legal_name"], c["billing_email"] or "", c["province"], c["address"] or "")
        if p.kind == "customer_update":
            ledger.update_customer(con, cid, **customer_record_fields(c))
        written["customers"] += 1
    for p in q.items:
        if p.decision != "approved" or p.kind != "invoice":
            continue
        inv = p.payload
        c = inv["customer"]
        cust_id = ledger.upsert_customer(con, c["legal_name"], c["billing_email"] or "", c["province"],
                                         c["address"] or "")
        lines = [{"desc": ln["description"], "qty": ln["quantity"], "unit_cents": ln["unit_price_cents"],
                  "amount_cents": ln["amount_cents"], "contract_id": ln["contract_id"]} for ln in inv["lines"]]
        iid = ledger.create_invoice(con, number=inv["number"], customer_id=cust_id, period=inv["period"],
                                    issue_date=inv["issue_date"], due_date=inv["due_date"], lines=lines,
                                    tax_code=inv["tax_code"], tax_cents=inv["tax_cents"],
                                    po_number=inv.get("po_number"), currency="CAD",
                                    provincial_tax_cents=inv.get("provincial_tax_cents", 0),
                                    provincial_tax_code=inv.get("provincial_tax_code") or None)
        ledger.set_status(con, "invoices", iid, "approved")
        pdf = render_invoice(inv, plan.supplier, Path(out_dir) / "invoices" / f"{inv['number']}.pdf")
        written["invoices"] += 1
        written["pdfs"].append(str(pdf))
    con.commit()
    return written
