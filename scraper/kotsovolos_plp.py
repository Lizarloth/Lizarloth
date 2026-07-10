#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kotsovolos_plp.py  -  Pricedge: Kotsovolos.gr category fetcher (INSPECTION build)

Kotsovolos exposes a clean paginated JSON API (WebSphere Commerce search):
  /api/ext/getProductsByCategory
    ?params=pageNumber=N&pageSize=200&catalogId=10551&langId=-24&orderBy=7
    &catId=<category id>&storeId=10151&isCPage=false

catalogEntryView[] holds the products. price_EUR is the Offer (sale) price;
the price[] array also carries the Display (list) price. partNumber = sku,
manufacturer = brand, UserData[0].seo_url = product URL. No installment teaser
exists in these fields, so the financing-price trap can't apply.

Pure requests, no Selenium. Run on the PC:
    py kotsovolos_plp.py
    py kotsovolos_plp.py freezer
Output: kotsovolos_plp.xlsx  (close it in Excel before re-running!)
"""

import re
import sys
import time
import requests

API = "https://www.kotsovolos.gr/api/ext/getProductsByCategory"
SITE_BASE = "https://www.kotsovolos.gr/"

# Static query constants (observed from the live site).
CATALOG_ID = "10551"
LANG_ID = "-24"
STORE_ID = "10151"
ORDER_BY = "7"
PAGE_SIZE = 200

# Category -> catId, all confirmed from Kotsovolos's own category pages.
CATEGORIES = {
    "fridge_freezer": "35822",
    "fridges":        "35820",
    "side_by_side":   "35821",
    "freezer":        "35819",
    "mini_bar":       "35232",
    "wine_cooler":    "35740",
    "built_in":       "35225",
}

# Laundry department. Kotsovolos category pages are addressed by numeric catId
# in the API, but we only have the SEO URLs — resolve_cat_id() fetches the page
# and extracts the catId at runtime (cached), so these can stay as URLs.
LAUNDRY = {
    "washing_machine": "https://www.kotsovolos.gr/household-appliances/washing-machines/washing-machines",
    "washer_dryer":    "https://www.kotsovolos.gr/household-appliances/washing-machines/washing-machines-dryers",
    "dryer":           "https://www.kotsovolos.gr/household-appliances/washing-machines/dryers",
}
LAUNDRY_COLS = ["site", "category", "department", "sku_id", "brand", "name",
                "price", "list_price", "energy", "dry_energy", "wash_kg",
                "dry_kg", "rpm", "programs", "steam", "heat_pump", "condenser",
                "loader", "dimensions", "image", "url"]
_CATID_CACHE = {}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "Referer": "https://www.kotsovolos.gr/",
    "X-Requested-With": "XMLHttpRequest",
}

# spec mining from name + shortDescription
_CAP = re.compile(r"(\d{2,4})\s*(?:lt|Lt|LT|λίτρα|λτ|lίτρα)\b")
_COOLING = ["Full No Frost", "Total No Frost", "No Frost",
            "Less Frost", "Low Frost"]
_ENERGY = re.compile(r"ενεργειακ\w*\s*(?:κλάση)?\s*[:\-]?\s*([A-G]\+{0,3})", re.I)


def price_to_float(v):
    if v is None:
        return None
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def mine_specs(text):
    cap = ""
    m = _CAP.search(text)
    if m:
        cap = m.group(1) + " Lt"
    cooling = ""
    low = text.lower()
    for c in _COOLING:
        if c.lower() in low:
            cooling = c
            break
    energy = ""
    m = _ENERGY.search(text)
    if m:
        energy = m.group(1).upper()
    return cap, cooling, energy


# ---- laundry spec mining (text-based, like the fridge miner) ----
_KG = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)", re.I)
# washer-dryer capacities almost always print as "X/Ykg" or "Xkg/Ykg" in the
# name -> parse the slash directly (wash = left, dry = right).
_WD_SLASH = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)?\s*/\s*(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)", re.I)
# rpm: tolerate the Greek thousands dot/comma so "1.400 στροφές" -> 1400
# (not 400).
_RPM = re.compile(r"((?:\d[.,])?\d{3,4})\s*(?:στροφ|rpm|σ\.?α\.?λ|σαλ)", re.I)
_PROG = re.compile(r"(\d{1,2})\s*προγρ", re.I)


def _fmtkg(x):
    return str(int(x)) if float(x).is_integer() else str(x)


# ---- structured attributes mining (Kotsovolos ships a full attributes[] array
#      with clean Greek labels -> far better than text for energy/rpm/programs) -
_YES = re.compile(r"διαθ|^ναι|yes|true", re.I)


def _attr_map(entry):
    out = {}
    for a in entry.get("attributes", []) or []:
        if not isinstance(a, dict):
            continue
        nm = a.get("name") or a.get("identifier")
        if not nm:
            continue
        vals = a.get("values")
        if isinstance(vals, list) and vals and isinstance(vals[0], dict):
            out[nm] = str(vals[0].get("value", "")).strip()
        elif a.get("value") not in (None, ""):
            out[nm] = str(a.get("value")).strip()
    return out


def _kg_of(s):
    m = re.search(r"\d{1,2}(?:[.,]\d)?", str(s))
    return m.group(0).replace(",", ".") if m else ""


def laundry_from_attrs(attrs):
    def g(*names):
        for n in names:
            if attrs.get(n):
                return attrs[n]
        return ""
    rpm_raw = g("Στροφές σε (rpm)", "Στροφές (rpm)", "Εύρος Στροφών σε (rpm)")
    load_raw = g("Τύπος πλυντηρίου", "Τύπος Φόρτωσης", "Τρόπος Φόρτωσης").lower()
    loader = "front" if "εμπρ" in load_raw else "top" if "άνω" in load_raw or "ανω" in load_raw else ""
    return {
        "wash_kg": _kg_of(g("Χωρητικότητα Πλύσης (kg)", "Εύρος Χωρητικότητας Πλύσης (kg)")),
        "dry_kg":  _kg_of(g("Χωρητικότητα Στεγνώματος (kg)", "Εύρος Χωρητικότητας Στεγνώματος (kg)")),
        "energy":  g("Ενεργειακή Κλάση", "Ενεργειακή κλάση", "Ενεργειακή Κλάση Πλύσης").upper()[:4],
        "dry_energy": g("Ενεργειακή Κλάση Στεγνώματος", "Ενεργειακή Κλάση (Στέγνωμα)").upper()[:4],
        "rpm": re.sub(r"\D", "", rpm_raw.split("έως")[-1]) if rpm_raw else "",
        "programs": re.sub(r"\D", "", g("Προγράμματα", "Αριθμός Προγραμμάτων")),
        "loader": loader,
        "steam": "Διαθέτει" if _YES.search(g("Πρόγραμμα Ατμού") or "") else "",
        "heat_pump": "Διαθέτει" if _YES.search(g("Αντλία Θερμότητας") or "") or "αντλία" in (g("Τύπος Στεγνωτηρίου", "Τεχνολογία Στεγνώματος") or "").lower() else "",
        "condenser": "Διαθέτει" if _YES.search(g("Σύστημα Συμπύκνωσης", "Τύπος Συμπύκνωσης") or "") or "συμπύκν" in (g("Τύπος Στεγνωτηρίου", "Τεχνολογία Στεγνώματος") or "").lower() else "",
        "dimensions": g("Διάσταση ΥxΠxΒ (cm)", "Διαστάσεις"),
    }


def mine_laundry(text, cat_name):
    low = text.lower()
    kgs = [float(m.group(1).replace(",", ".")) for m in _KG.finditer(text)]
    kgs = [k for k in kgs if 3 <= k <= 20]          # plausible laundry kg
    wash_kg = dry_kg = ""
    if cat_name == "dryer":
        if kgs:
            dry_kg = _fmtkg(kgs[0])
    elif cat_name == "washer_dryer":
        m = _WD_SLASH.search(text)
        if m:
            wash_kg, dry_kg = _fmtkg(float(m.group(1).replace(",", "."))), _fmtkg(float(m.group(2).replace(",", ".")))
        elif len(kgs) >= 2:
            wash_kg, dry_kg = _fmtkg(max(kgs)), _fmtkg(min(kgs))
        elif kgs:
            wash_kg = _fmtkg(kgs[0])
    else:                                            # washing machine
        if kgs:
            wash_kg = _fmtkg(max(kgs))
    rpm = ""
    m = _RPM.search(text)
    if m:
        rpm = re.sub(r"\D", "", m.group(1))
    energy = ""
    m = _ENERGY.search(text)
    if m:
        energy = m.group(1).upper()
    progs = ""
    m = _PROG.search(text)
    if m:
        progs = m.group(1)
    return {
        "wash_kg": wash_kg, "dry_kg": dry_kg, "rpm": rpm, "energy": energy,
        "programs": progs,
        "heat_pump": "Διαθέτει" if "αντλία θερμότητας" in low else "",
        "condenser": "Διαθέτει" if "συμπύκνωσ" in low else "",
    }


def resolve_cat_id(session, url):
    """Fetch a Kotsovolos category page and extract its numeric catId so the
    laundry SEO URLs can drive the same getProductsByCategory API."""
    if url in _CATID_CACHE:
        return _CATID_CACHE[url]
    try:
        html = session.get(url, timeout=40).text
    except Exception as e:
        print(f"  ! could not load {url}: {e}")
        return None
    for pat in (r'catId["\s:=]+(\d{3,6})', r'categoryId["\s:=]+(\d{3,6})',
                r'"catalogGroupId"\s*:\s*"?(\d{3,6})'):
        m = re.search(pat, html)
        if m:
            _CATID_CACHE[url] = m.group(1)
            return m.group(1)
    print(f"  ! catId not found on {url} (paste me the page's catId and I'll hardcode it)")
    return None


def dump_entry(session, cat_name="washing_machine"):
    """Diagnostic: surface the `attributes` array (and the SKU's attributes),
    which is where structured specs like energy class / rpm live."""
    import json
    src = LAUNDRY.get(cat_name) or CATEGORIES.get(cat_name)
    cid = resolve_cat_id(session, src) if cat_name in LAUNDRY else src
    if not cid:
        print("  no catId; cannot dump"); return
    data = fetch_page(session, cid, 1)
    entries = data.get("catalogEntryView", []) or []
    if not entries:
        print("  no entries returned"); return
    e = entries[0]
    print(f"\n=== {e.get('name','?')} ({cat_name}) ===")

    def show_attrs(attrs, where):
        print(f"\n-- {where}: {len(attrs) if isinstance(attrs, list) else 'n/a'} attributes --")
        if not isinstance(attrs, list):
            print("  (not a list):", repr(attrs)[:300]); return
        for a in attrs:
            if not isinstance(a, dict):
                print("  ", repr(a)[:200]); continue
            nm = a.get("name") or a.get("identifier") or a.get("displayName") or "?"
            vals = a.get("values") or a.get("value") or a.get("attributeValueDisplay") or ""
            if isinstance(vals, list):
                vals = ", ".join(str(v.get("value", v) if isinstance(v, dict) else v) for v in vals[:5])
            flag = ""
            blob = f"{nm} {vals}".lower()
            if any(k in blob for k in ("ενεργ", "energy", "στροφ", "rpm", "κιλ", "πρόγρ", "προγρ", "αντλ", "συμπ")):
                flag = "   <<<"
            print(f"  {nm!r}: {vals!r}{flag}")

    show_attrs(e.get("attributes"), "entry.attributes")
    skus = e.get("sKUs") or []
    if skus and isinstance(skus[0], dict):
        show_attrs(skus[0].get("attributes"), "sKUs[0].attributes")
    print("\n-- raw attributes JSON (first 4000 chars) --")
    print(json.dumps(e.get("attributes", []), ensure_ascii=False, indent=1)[:4000])


def parse_entry(entry, cat_name, dept="cooling"):
    name = entry.get("name", "") or ""
    desc = entry.get("shortDescription", "") or ""
    sale = price_to_float(entry.get("price_EUR"))
    listp = None
    for p in entry.get("price", []) or []:
        if p.get("usage") == "Display":
            listp = price_to_float(p.get("value"))
        if p.get("usage") == "Offer" and sale is None:
            sale = price_to_float(p.get("value"))

    seo = ""
    ud = entry.get("UserData") or []
    if isinstance(ud, list) and ud and isinstance(ud[0], dict):
        seo = ud[0].get("seo_url", "") or ""
    url = SITE_BASE + seo.lstrip("/") if seo else ""

    img = (entry.get("thumbnail") or entry.get("fullImage")
           or entry.get("thumbnailRaw") or entry.get("auxiliaryImage") or "")
    if isinstance(img, list):
        img = (img[0] if img else "") or ""
    if isinstance(img, dict):
        img = img.get("url", "") or img.get("src", "")
    if img and img.startswith("/"):
        img = SITE_BASE + img.lstrip("/")

    # identity fields: mfPartNumber is WebSphere Commerce's manufacturer part
    # number; EAN/barcode may sit in the attributes[] array under a Greek or
    # English label — scan for it.
    mpn = str(entry.get("mfPartNumber", "") or "").strip()
    attrs_all = _attr_map(entry)
    ean = ""
    for k, v in attrs_all.items():
        if re.search(r"ean|barcode|gtin|γραμμωτ", str(k), re.I):
            digits = re.sub(r"\D", "", str(v))
            if len(digits) >= 8:
                ean = digits
                break

    def _g(*names):
        for nm in names:
            if attrs_all.get(nm):
                return attrs_all[nm]
        return ""

    # availability: 'buyable' is WebSphere's purchasable flag for the listing
    buyable = str(entry.get("buyable", "")).lower()
    avail = "InStock" if buyable == "true" else ("OutOfStock" if buyable == "false" else "")

    base = {
        "site": "kotsovolos",
        "category": cat_name,
        "sku_id": str(entry.get("partNumber", "")),
        "ean": ean,
        "mpn": mpn,
        "brand": entry.get("manufacturer", "") or "",
        "name": name,
        "price": sale,
        "list_price": listp,
        "availability": avail,
        "image": img,
        "url": url,
    }

    if dept == "laundry":
        base["department"] = "laundry"
        la = laundry_from_attrs(attrs_all)                  # structured, authoritative
        tla = mine_laundry(f"{name} {desc}", cat_name)      # text fallback
        for k in ("wash_kg", "dry_kg", "energy", "dry_energy", "rpm",
                  "programs", "steam", "heat_pump", "condenser", "dimensions"):
            base[k] = la.get(k) or tla.get(k, "")
        base["loader"] = la.get("loader") or ("front" if cat_name == "washer_dryer" else "")
        return base

    # cooling: structured attributes first (same source the laundry path
    # already trusts — this is where dimensions/colour/noise live, which the
    # old text miner never captured), then the text miner as fallback.
    cap, cooling, energy = mine_specs(f"{name} {desc}")
    base.update({
        "energy":   (_g("Ενεργειακή Κλάση", "Ενεργειακή κλάση") or energy).strip()[:4],
        "capacity": _g("Καθαρή Συνολική Χωρητικότητα (lt)", "Συνολική Χωρητικότητα (lt)",
                       "Συνολική χωρητικότητα", "Καθαρή χωρητικότητα") or cap,
        "cooling":  _g("Τύπος Ψύξης", "Τύπος ψύξης") or cooling,
        "dimensions": _g("Διάσταση ΥxΠxΒ (cm)", "Διαστάσεις (ΥxΠxΒ)", "Διαστάσεις"),
        "color":    _g("Χρώμα"),
        "noise":    _g("Επίπεδο Θορύβου (dB)", "Επίπεδα θορύβου"),
    })
    return base


def fetch_page(session, cat_id, page_number):
    inner = (f"pageNumber={page_number}&pageSize={PAGE_SIZE}"
             f"&catalogId={CATALOG_ID}&langId={LANG_ID}&orderBy={ORDER_BY}")
    params = {
        "params": inner,
        "catId": cat_id,
        "storeId": STORE_ID,
        "isCPage": "false",
    }
    r = session.get(API, params=params, timeout=40)
    r.raise_for_status()
    return r.json()


def collect_category(session, cat_id, cat_name, debug=True, dept="cooling"):
    merged = {}
    total = None
    page = 1
    while page <= 50:
        try:
            data = fetch_page(session, cat_id, page)
        except Exception as e:
            print(f"  ! page {page} failed: {e}")
            break
        if total is None:
            total = data.get("recordSetTotal")
        entries = data.get("catalogEntryView", []) or []
        new = 0
        for e in entries:
            row = parse_entry(e, cat_name, dept=dept)
            if row["sku_id"] and row["sku_id"] not in merged:
                merged[row["sku_id"]] = row
                new += 1
        if debug:
            print(f"  page {page}: {len(entries)} entries, +{new} new "
                  f"-> {len(merged)}/{total or '?'}")
        if not entries or new == 0:
            break
        if total and len(merged) >= total:
            break
        if data.get("recordSetComplete") is True:
            break
        page += 1
        time.sleep(0.4)
    return list(merged.values()), total


def write_excel(rows, path="kotsovolos_plp.xlsx", cols=None, title="kotsovolos"):
    if cols is None:
        cols = ["site", "category", "sku_id", "brand", "name", "price",
                "list_price", "energy", "capacity", "cooling", "image", "url"]
    try:
        from openpyxl import Workbook
    except ImportError:
        import csv
        path = path.replace(".xlsx", ".csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows -> {path} (CSV fallback)")
        return path
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(cols)
    for row in rows:
        ws.append([row.get(c, "") for c in cols])
    try:
        wb.save(path)
    except PermissionError:
        print(f"\n  !! {path} is open in Excel. Close it and re-run.\n")
        sys.exit(1)
    csv_path = path.replace(".xlsx", ".csv")
    import csv
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
    except PermissionError:
        csv_path = "(skipped - open)"
    print(f"Wrote {len(rows)} rows -> {path}  (+ {csv_path})")
    return path


def _run(session, targets, source, dept, out_path, cols, title):
    all_rows, per_cat = {}, {}
    for cat_name in targets:
        ident = source[cat_name]
        cat_id = ident if dept == "cooling" else resolve_cat_id(session, ident)
        if not cat_id:
            print(f"  skip {cat_name}: no catId"); continue
        print(f"\n=== {cat_name}  (catId {cat_id})  [{dept}] ===")
        rows, total = collect_category(session, cat_id, cat_name, dept=dept)
        for r in rows:
            all_rows.setdefault(r["sku_id"], r)
        per_cat[cat_name] = (len(rows), total)

    rows = list(all_rows.values())
    print("\n" + "=" * 48)
    print(f"TOTAL unique {dept} products: {len(rows)}")
    for cat_name, (n, total) in per_cat.items():
        tag = "full" if (total and n >= total) else f"of ~{total}"
        print(f"   {cat_name:16} {n} ({tag})")
    bad = [r for r in rows if not r["price"] or r["price"] < 35]
    print(f"  price sanity: {'all OK' if not bad else str(len(bad))+' suspicious'}")
    mine = [r for r in rows if (r["brand"] or "").upper() in {"WHIRLPOOL", "INDESIT", "BEKO"}]
    print(f"  your brands (Whirlpool/Indesit/Beko): {len(mine)}")
    if dept == "laundry":
        fl = lambda k: sum(1 for r in rows if r.get(k))
        print(f"  pillars: wash_kg {fl('wash_kg')}, dry_kg {fl('dry_kg')}, rpm {fl('rpm')}, "
              f"energy {fl('energy')}, programs {fl('programs')} /{len(rows)}")
    else:
        cov = sum(1 for r in rows if r.get("capacity") or r.get("cooling"))
        print(f"  spec mining hit {cov}/{len(rows)} cards")
    write_excel(rows, path=out_path, cols=cols, title=title)


def main():
    # py kotsovolos_plp.py                 -> fridges (kotsovolos_plp.xlsx)
    # py kotsovolos_plp.py laundry         -> all laundry (kotsovolos_laundry.xlsx)
    # py kotsovolos_plp.py washing_machine -> one laundry type
    args = sys.argv[1:]
    if args and args[0] == "dumpkeys":
        session = requests.Session(); session.headers.update(HEADERS)
        dump_entry(session, args[1] if len(args) > 1 else "washing_machine")
        return
    if args == ["laundry"]:
        targets = list(LAUNDRY)
    else:
        targets = args if args else list(CATEGORIES)
    laundry_t = [t for t in targets if t in LAUNDRY]
    fridge_t = [t for t in targets if t in CATEGORIES]
    unknown = [t for t in targets if t not in CATEGORIES and t not in LAUNDRY]
    if unknown:
        print(f"Unknown {unknown}. Fridge:{list(CATEGORIES)} Laundry:{list(LAUNDRY)} (or 'laundry')")
        sys.exit(1)

    session = requests.Session()
    session.headers.update(HEADERS)
    if fridge_t:
        _run(session, fridge_t, CATEGORIES, "cooling", "kotsovolos_plp.xlsx", None, "kotsovolos")
    if laundry_t:
        _run(session, laundry_t, LAUNDRY, "laundry", "kotsovolos_laundry.xlsx",
             LAUNDRY_COLS, "kotsovolos_laundry")


if __name__ == "__main__":
    main()