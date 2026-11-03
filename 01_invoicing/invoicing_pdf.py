"""Render a GST/HST invoice PDF with reportlab.

The layout carries every field the CRA lists for invoices of $500 or more (the strictest tier),
whatever the amount: supplier name and GST/HST registration number, invoice date, customer name,
description of each supply, payment terms, the GST/HST charged and the total. When QST, PST or
RST is charged it gets its own line and the supplier's registration number for that tax is
printed under the GST/HST number.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from shared.tax import fmt


def render_invoice(inv: dict, supplier: dict, path: Path, *, draft: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Uncompressed so the text is greppable in tests; files stay small.
    c = canvas.Canvas(str(path), pagesize=LETTER, pageCompression=0)
    c.setTitle(f"Invoice {inv['number']}")
    c.setAuthor(supplier["name"])
    w, h = LETTER
    x0, y = 54, h - 60

    if draft:
        c.saveState()
        c.setFont("Helvetica-Bold", 60)
        c.setFillGray(0.88)
        c.translate(w / 2, h / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, "DRAFT - NOT ISSUED")
        c.restoreState()

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x0, y, supplier["name"])
    c.setFont("Helvetica", 9)
    c.drawString(x0, y - 14, supplier.get("address", ""))
    c.drawString(x0, y - 26, f"GST/HST registration no.: {supplier.get('gst_hst_number') or 'NOT ON FILE'}")
    c.drawString(x0, y - 38, f"Email: {supplier.get('email', '')}")
    p_code = inv.get("provincial_tax_code") if inv.get("provincial_tax_cents") else ""
    if p_code:
        c.drawString(x0, y - 50, f"{p_code} registration no.: {inv.get('provincial_tax_number') or 'NOT ON FILE'}")

    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(w - x0, y, "INVOICE")
    c.setFont("Helvetica", 9)
    meta = [
        ("Invoice no.", inv["number"]),
        ("Invoice date", inv["issue_date"]),
        ("Billing period", inv["period"]),
        ("Payment terms", inv["payment_terms"]),
        ("Due date", inv["due_date"]),
        ("PO number", inv.get("po_number") or "-"),
    ]
    for i, (k, v) in enumerate(meta):
        c.drawRightString(w - x0 - 110, y - 18 - 12 * i, f"{k}:")
        c.drawRightString(w - x0, y - 18 - 12 * i, str(v))

    y -= 110
    cust = inv["customer"]
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, y, "Bill to")
    c.setFont("Helvetica", 9)
    c.drawString(x0, y - 14, cust.get("legal_name") or "")
    c.drawString(x0, y - 26, cust.get("address") or "")
    c.drawString(x0, y - 38, cust.get("billing_email") or "")
    c.drawString(x0, y - 50, f"Place of supply: {inv.get('place_of_supply') or 'unknown'}")

    y -= 80
    c.setFont("Helvetica-Bold", 9)
    cols = [x0, x0 + 300, x0 + 370, w - x0]
    c.drawString(cols[0], y, "Description")
    c.drawRightString(cols[1] + 40, y, "Qty")
    c.drawRightString(cols[2] + 60, y, "Unit price")
    c.drawRightString(cols[3], y, "Amount")
    c.line(x0, y - 4, w - x0, y - 4)
    c.setFont("Helvetica", 9)
    y -= 18
    for ln in inv["lines"]:
        c.drawString(cols[0], y, ln["description"][:70])
        c.drawRightString(cols[1] + 40, y, ln["quantity"])
        c.drawRightString(cols[2] + 60, y, fmt(ln["unit_price_cents"]))
        c.drawRightString(cols[3], y, fmt(ln["amount_cents"]))
        y -= 14
        if y < 160:
            c.showPage()
            c.setFont("Helvetica", 9)
            y = h - 60

    y -= 6
    c.line(cols[2], y + 8, w - x0, y + 8)
    totals = [
        ("Subtotal (taxable supplies)", fmt(inv["subtotal_cents"])),
        (f"{inv['tax_label']} ({inv['place_of_supply']})" if inv["tax_label"] else "GST/HST", fmt(inv["tax_cents"])),
    ]
    if p_code:
        totals.append((f"{inv['provincial_tax_label']} ({inv['place_of_supply']})", fmt(inv["provincial_tax_cents"])))
    totals.append(("Total (CAD)", fmt(inv["total_cents"])))
    for i, (k, v) in enumerate(totals):
        c.setFont("Helvetica-Bold" if i == len(totals) - 1 else "Helvetica", 9)
        c.drawRightString(cols[2] + 60, y - 14 * i, k)
        c.drawRightString(cols[3], y - 14 * i, v)

    c.setFont("Helvetica", 8)
    taxes = f"GST/HST and {p_code}" if p_code else "GST/HST"
    c.drawString(x0, 90, "All supplies on this invoice are taxable at the rates shown. "
                         f"Prices exclude {taxes}, shown separately.")
    c.drawString(x0, 78, f"Please quote {inv['number']} with your payment. Amounts in Canadian dollars.")
    c.save()
    return path
