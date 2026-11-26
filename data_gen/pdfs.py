"""Render invoice PDFs (3 layouts) with reportlab, and fake a scanner with pymupdf + Pillow."""

import io
import random
from datetime import date

import numpy as np
import pymupdf
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from data_gen.scenarios import BILL_TO, InvoiceSpec

W, H = letter
MARGIN = 48

# (invoice number label, date label, PO label, date format, font, bold font)
LABELS = (
    ("Invoice No.", "Date", "PO Number", "iso", "Helvetica", "Helvetica-Bold"),
    ("Inv #", "Inv Date", "Your PO", "long", "Courier", "Courier-Bold"),
    ("Invoice Number", "Invoice Date", "P.O. #", "dmy", "Times-Roman", "Times-Bold"),
)
BRAND_COLORS = (
    colors.HexColor("#1f4e79"),
    colors.HexColor("#8a2b2b"),
    colors.HexColor("#2d6a4f"),
    colors.HexColor("#5b3a8c"),
)


def fmt_date(d: date, style: str) -> str:
    if style == "iso":
        return d.isoformat()
    if style == "long":
        return f"{d:%b} {d.day}, {d.year}"
    return f"{d.day:02d}/{d.month:02d}/{d.year}"  # day first


def _logo(c: Canvas, x: float, y: float, spec: InvoiceSpec) -> None:
    """Simple vector logo: coloured badge with the vendor's initials."""
    color = BRAND_COLORS[sum(map(ord, spec.vendor.name)) % len(BRAND_COLORS)]
    initials = "".join(w[0] for w in spec.vendor.name.split()[:2]).upper()
    c.setFillColor(color)
    if spec.layout == 1:
        c.roundRect(x, y, 56, 56, 10, stroke=0, fill=1)
    else:
        c.circle(x + 28, y + 28, 28, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(x + 28, y + 21, initials)
    c.setFillColor(colors.black)


def _money(x: object, currency: str) -> str:
    return f"{x:,.2f}"


def render_invoice(spec: InvoiceSpec) -> bytes:
    """Return the text-based PDF for `spec` (deterministic bytes)."""
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=letter, invariant=1)
    num_lbl, date_lbl, po_lbl, date_style, font, bold = LABELS[spec.layout]
    v = spec.vendor
    cur = v.currency
    right = spec.layout == 1

    # ---- header: logo + vendor block (left or right)
    top = H - MARGIN
    logo_x = W - MARGIN - 56 if right else MARGIN
    _logo(c, logo_x, top - 56, spec)
    text_x = W - MARGIN - 56 - 12 if right else MARGIN + 56 + 12
    align = c.drawRightString if right else c.drawString
    c.setFont(bold, 15)
    align(text_x, top - 14, v.name)
    c.setFont(font, 9)
    for i, line in enumerate([*v.address, v.phone]):
        align(text_x, top - 28 - i * 11, line)

    # ---- title + meta block
    y = top - 100
    c.setFont(bold, 22)
    c.drawString(MARGIN, y, "INVOICE" if spec.layout != 1 else "Invoice")
    meta = [
        (num_lbl, spec.invoice_number),
        (date_lbl, fmt_date(spec.invoice_date, date_style)),
    ]
    if spec.po_number:
        meta.append((po_lbl, spec.po_number))
    meta += [("Terms", v.payment_terms), ("Currency", cur)]
    mx = W - MARGIN - 190
    c.setFont(font, 10)
    for i, (k, val) in enumerate(meta):
        c.setFont(font, 10)
        c.drawString(mx, y - i * 14, f"{k}:")
        c.setFont(bold, 10)
        c.drawString(mx + 85, y - i * 14, val)

    # ---- bill to
    c.setFont(bold, 10)
    c.drawString(MARGIN, y - 34, "Bill To:")
    c.setFont(font, 10)
    c.drawString(MARGIN, y - 48, BILL_TO)
    c.drawString(MARGIN, y - 61, "Accounts Payable")

    # ---- lines
    y -= 105
    if spec.layout == 0:
        y = _table_a(c, spec, y, font, bold)
    elif spec.layout == 1:
        y = _plain_b(c, spec, y, font, bold)
    else:
        y = _table_c(c, spec, y, font, bold)

    # ---- totals
    y -= 14
    tx = W - MARGIN - 200
    rows = [("Subtotal", spec.subtotal)]
    if cur == "CAD":
        rows.append(("HST (13%)", spec.tax))
    rows.append((f"Total {cur}", spec.total))
    for i, (k, val) in enumerate(rows):
        last = i == len(rows) - 1
        c.setFont(bold if last else font, 11 if last else 10)
        c.drawString(tx, y - i * 16, k)
        c.drawRightString(W - MARGIN, y - i * 16, _money(val, cur))
    c.setFont(font, 8)
    c.drawString(
        MARGIN,
        MARGIN,
        f"Payment terms: {v.payment_terms}. Please quote the invoice number on payment.",
    )
    c.showPage()
    c.save()
    return buf.getvalue()


def _table_a(c: Canvas, spec: InvoiceSpec, y: float, font: str, bold: str) -> float:
    cols = (MARGIN + 4, MARGIN + 80, W - MARGIN - 150, W - MARGIN - 80, W - MARGIN - 4)
    c.setFillColor(colors.HexColor("#e6e6e6"))
    c.rect(MARGIN, y - 4, W - 2 * MARGIN, 16, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont(bold, 9)
    c.drawString(cols[0], y, "Part No")
    c.drawString(cols[1], y, "Description")
    c.drawRightString(cols[2], y, "Qty")
    c.drawRightString(cols[3], y, "Unit Price")
    c.drawRightString(cols[4], y, "Amount")
    c.setFont(font, 9)
    y -= 18
    for ln in spec.lines:
        c.drawString(cols[0], y, ln.part_no)
        c.drawString(cols[1], y, ln.description[:48])
        c.drawRightString(cols[2], y, str(ln.qty))
        c.drawRightString(cols[3], y, f"{ln.unit_price:,.2f}")
        c.drawRightString(cols[4], y, f"{ln.line_total:,.2f}")
        y -= 15
    c.line(MARGIN, y + 8, W - MARGIN, y + 8)
    return y


def _table_c(c: Canvas, spec: InvoiceSpec, y: float, font: str, bold: str) -> float:
    cols = (MARGIN + 24, MARGIN + 40, MARGIN + 120, W - MARGIN - 80, W - MARGIN - 4)
    c.setFont(bold, 10)
    c.drawRightString(cols[0], y, "Qty")
    c.drawString(cols[1], y, "Item")
    c.drawString(cols[2], y, "Description")
    c.drawRightString(cols[3], y, "Price")
    c.drawRightString(cols[4], y, "Total")
    c.line(MARGIN, y - 4, W - MARGIN, y - 4)
    c.setFont(font, 10)
    y -= 20
    for ln in spec.lines:
        c.drawRightString(cols[0], y, str(ln.qty))
        c.drawString(cols[1], y, ln.part_no)
        c.drawString(cols[2], y, ln.description[:42])
        c.drawRightString(cols[3], y, f"{ln.unit_price:,.2f}")
        c.drawRightString(cols[4], y, f"{ln.line_total:,.2f}")
        c.setStrokeColor(colors.lightgrey)
        c.line(MARGIN, y - 4, W - MARGIN, y - 4)
        c.setStrokeColor(colors.black)
        y -= 17
    return y


def _plain_b(c: Canvas, spec: InvoiceSpec, y: float, font: str, bold: str) -> float:
    c.setFont(bold, 9)
    c.drawString(MARGIN, y, "Items shipped:")
    c.setFont(font, 8)
    y -= 16
    for ln in spec.lines:
        c.drawString(
            MARGIN,
            y,
            f"{ln.qty} x {ln.part_no} - {ln.description[:40]} @ {ln.unit_price:,.2f} = {ln.line_total:,.2f}",
        )
        y -= 14
    return y


def scan(pdf: bytes, seed: str) -> bytes:
    """Fake a scanner: rasterise, rotate 1-3 degrees, add light noise, image-only PDF."""
    rng = random.Random(seed)
    src = pymupdf.open(stream=pdf, filetype="pdf")
    page = src[0]
    pix = page.get_pixmap(dpi=150)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    angle = rng.uniform(1.0, 3.0) * rng.choice((-1, 1))
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
    arr = np.asarray(img, dtype=np.float32)
    noise = np.random.default_rng(rng.getrandbits(32)).normal(0, 7, arr.shape)
    arr = np.clip(arr * 0.97 + noise, 0, 255).astype(np.uint8)
    jpeg = io.BytesIO()
    Image.fromarray(arr).save(jpeg, format="JPEG", quality=70)
    out = pymupdf.open()
    new = out.new_page(width=page.rect.width, height=page.rect.height)
    new.insert_image(new.rect, stream=jpeg.getvalue())
    out.set_metadata({})
    return out.tobytes(deflate=True, no_new_id=True)
