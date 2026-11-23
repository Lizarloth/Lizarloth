import io
import json
import csv
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


# ── CSV ──────────────────────────────────────────────────────────────────

def export_price_history_csv(products, history_rows) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Product", "Site", "URL", "Price (€)", "Was Price (€)", "Discount %", "Availability", "Scraped At"])
    product_map = {p.id: p for p in products}   # O(1) lookups; the linear scan per row was quadratic
    for h in history_rows:
        p = product_map.get(h.product_id)
        disc = ""
        if h.price and h.old_price and h.old_price > h.price + 0.5:
            disc = round((1 - h.price / h.old_price) * 100, 1)
        writer.writerow([
            p.name if p else "",
            p.site if p else "",
            p.url if p else "",
            h.price or "",
            h.old_price or "",
            disc,
            h.availability or "",
            h.scraped_at.strftime("%Y-%m-%d %H:%M") if h.scraped_at else "",
        ])
    return output.getvalue().encode("utf-8-sig")  # utf-8-sig for Excel compat


def export_alerts_csv(alerts) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Product", "Site", "Condition", "Threshold (€)", "Price at Alert (€)", "Triggered At", "Email Sent"])
    for a in alerts:
        writer.writerow([
            a.product_name, a.site, a.condition,
            a.threshold, a.current_price,
            a.triggered_at.strftime("%Y-%m-%d %H:%M") if a.triggered_at else "",
            "Yes" if a.email_sent else "No",
        ])
    return output.getvalue().encode("utf-8-sig")


def export_competitors_csv(products, latest_prices: dict) -> bytes:
    """latest_prices: {product_id: {site: price}}"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Product", "Kotsovolos (€)", "Public.gr (€)", "Plaisio (€)", "Best Price (€)", "Best Site"])
    for p in products:
        prices = latest_prices.get(p.id, {})
        vals = {k: v for k, v in prices.items() if v is not None}
        best_site = min(vals, key=vals.get) if vals else ""
        best_price = vals[best_site] if best_site else ""
        writer.writerow([
            p.name,
            prices.get("kotsovolos", ""),
            prices.get("public", ""),
            prices.get("plaisio", ""),
            best_price,
            best_site,
        ])
    return output.getvalue().encode("utf-8-sig")


# ── EXCEL ─────────────────────────────────────────────────────────────────

def _style_header_row(ws, row_num: int, num_cols: int):
    fill = PatternFill("solid", fgColor="0D0D0D")
    font = Font(color="C8F060", bold=True, name="Courier New", size=10)
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row_num, column=col)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="left", vertical="center")


def _auto_width(ws):
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 50)


def export_full_excel(products, history_rows, alert_rows) -> bytes:
    wb = openpyxl.Workbook()

    # ── Sheet 1: Price History ──
    ws1 = wb.active
    ws1.title = "Price History"
    headers1 = ["Product", "Site", "Price (€)", "Was Price (€)", "Discount %", "Availability", "Scraped At"]
    ws1.append(headers1)
    _style_header_row(ws1, 1, len(headers1))
    ws1.row_dimensions[1].height = 20

    product_map = {p.id: p for p in products}
    for h in history_rows:
        p = product_map.get(h.product_id)
        disc = ""
        if h.price and h.old_price and h.old_price > h.price + 0.5:
            disc = round((1 - h.price / h.old_price) * 100, 1)
        ws1.append([
            p.name if p else "",
            p.site if p else "",
            h.price,
            h.old_price,
            disc,
            h.availability or "",
            h.scraped_at.strftime("%Y-%m-%d %H:%M") if h.scraped_at else "",
        ])
    _auto_width(ws1)

    # ── Sheet 2: Competitor Comparison ──
    ws2 = wb.create_sheet("Competitor Comparison")
    headers2 = ["Product", "Kotsovolos (€)", "Public.gr (€)", "Plaisio (€)", "Cheapest Site", "Best Price (€)", "Your Saving"]
    ws2.append(headers2)
    _style_header_row(ws2, 1, len(headers2))

    # Group latest price per product per site
    latest: dict[int, dict[str, float]] = {}
    for h in sorted(history_rows, key=lambda x: x.scraped_at or datetime.min):
        p = product_map.get(h.product_id)
        if p and h.price:
            latest.setdefault(p.id, {})[p.site] = h.price

    for p in products:
        prices = latest.get(p.id, {})
        vals = {k: v for k, v in prices.items() if v}
        best_site = min(vals, key=vals.get) if vals else ""
        best_price = vals.get(best_site, "")
        your_price = vals.get(p.site, "")
        saving = round(your_price - best_price, 2) if your_price and best_price else ""
        ws2.append([
            p.name,
            prices.get("kotsovolos", ""),
            prices.get("public", ""),
            prices.get("plaisio", ""),
            best_site,
            best_price,
            saving,
        ])
    _auto_width(ws2)

    # ── Sheet 3: Alert Log ──
    ws3 = wb.create_sheet("Alert Log")
    headers3 = ["Product", "Site", "Condition", "Threshold (€)", "Price at Alert (€)", "Triggered At", "Email Sent"]
    ws3.append(headers3)
    _style_header_row(ws3, 1, len(headers3))
    for a in alert_rows:
        ws3.append([
            a.product_name, a.site, a.condition,
            a.threshold, a.current_price,
            a.triggered_at.strftime("%Y-%m-%d %H:%M") if a.triggered_at else "",
            "Yes" if a.email_sent else "No",
        ])
    _auto_width(ws3)

    # ── Sheet 4: Products ──
    ws4 = wb.create_sheet("Products")
    headers4 = ["ID", "Name", "Site", "URL", "Alert Threshold (€)", "Alert Email", "Active", "Added"]
    ws4.append(headers4)
    _style_header_row(ws4, 1, len(headers4))
    for p in products:
        ws4.append([
            p.id, p.name, p.site, p.url,
            p.alert_threshold, p.alert_email,
            "Yes" if p.active else "No",
            p.created_at.strftime("%Y-%m-%d") if p.created_at else "",
        ])
    _auto_width(ws4)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_RETAILER = {"kotsovolos": "Kotsovolos", "public": "Public", "plaisio": "Plaisio"}
_DEPT_ORDER = {"Cooling": 0, "Laundry": 1, "Dishwashing": 2}
_EUR = '#,##0.00 "€"'


def _brand(b):
    b = b or ""
    return b if len(b) <= 3 else b.title()      # LG, AEG, TCL stay upper case


def _month_label(m):
    return datetime.strptime(m, "%Y-%m").strftime("%b %Y")


def export_monthly_excel(rows, months) -> bytes:
    """rows: competitor models per retailer (see /api/export/monthly), each with
    dept, cat, brand, code, name, site, sku, active, months {YYYY-MM: price},
    closest (your model, its price)."""
    wb = openpyxl.Workbook()
    rows = sorted(rows, key=lambda r: (_DEPT_ORDER.get(r["dept"], 9), r["cat"] or "", r["brand"],
                                       r["code"] or r["name"], r["site"]))
    mcols = [_month_label(m) for m in months]

    # ── Sheet 1: one row per model per retailer ──
    ws = wb.active
    ws.title = "By retailer"
    head = ["Department", "Subcategory", "Brand", "Model code", "Product name", "Retailer",
            "Retailer SKU", "Listed now", "Your closest model", "Its price now"] + mcols
    ws.append(head)
    _style_header_row(ws, 1, len(head))
    for r in rows:
        ws.append([r["dept"], r["cat"], _brand(r["brand"]), r["code"], r["name"],
                   _RETAILER.get(r["site"], r["site"]), r["sku"] or "", "Yes" if r["active"] else "No",
                   r["closest"][0], r["closest"][1]] + [r["months"].get(m) for m in months])

    # ── Sheet 2: one row per model, the lowest retailer's monthly price ──
    ws2 = wb.create_sheet("By model")
    head2 = ["Department", "Subcategory", "Brand", "Model code", "Product name", "Retailers",
             "Your closest model"] + mcols
    ws2.append(head2)
    _style_header_row(ws2, 1, len(head2))
    models = {}
    for r in rows:
        key = (r["dept"], r["brand"], r["code"] or r["name"].upper())
        e = models.setdefault(key, {"r": r, "sites": [], "months": {}})
        e["sites"].append(_RETAILER.get(r["site"], r["site"]))
        for m, v in r["months"].items():
            e["months"][m] = min(v, e["months"].get(m, v))
    for e in models.values():
        r = e["r"]
        ws2.append([r["dept"], r["cat"], _brand(r["brand"]), r["code"], r["name"],
                    ", ".join(sorted(set(e["sites"]))), r["closest"][0]] + [e["months"].get(m) for m in months])

    for sheet, first_month, money in ((ws, 11, [10]), (ws2, 8, [])):
        _auto_width(sheet)
        sheet.column_dimensions["E"].width = 46
        sheet.freeze_panes = sheet.cell(row=2, column=first_month)
        sheet.auto_filter.ref = sheet.dimensions
        for col in money + list(range(first_month, first_month + len(months))):
            letter = get_column_letter(col)
            sheet.column_dimensions[letter].width = max(sheet.column_dimensions[letter].width or 0, 12)
            for row in sheet.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = _EUR

    # ── Sheet 3: how the numbers are made ──
    ws3 = wb.create_sheet("Notes")
    for line in [
        "Competitor monthly prices - Cooling, Laundry, Dishwashing",
        f"Generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC from Pricedge's stored price history.",
        "",
        "Competitors = every brand except Whirlpool, Indesit and Beko (and Hotpoint in Dishwashing).",
        "Monthly price = the median of that month's daily shelf prices at the retailer; a day's price",
        "is its last reading. The median keeps a one-day promo or a scraping glitch from setting it.",
        "Empty cell = the model wasn't seen at that retailer that month.",
        "The current month is month-to-date. The first month starts when tracking began.",
        "'By model' takes, per month, the lowest of the retailers' monthly prices.",
        "'Your closest model' = your model in the same subcategory with the nearest current price.",
        "Public: since its site blocked scraping (2026-11-13), some Public prices come from BestPrice.",
    ]:
        ws3.append([line])
    ws3.column_dimensions["A"].width = 100
    ws3["A1"].font = Font(bold=True, size=12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
