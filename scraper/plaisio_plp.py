#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plaisio_plp.py  -  Pricedge: Plaisio.gr category fetcher (INSPECTION build)

Plaisio is a Next.js app. The product list is exposed as a schema.org
ItemList in ld+json, BUT it's injected client-side after hydration -- it is
NOT in the raw HTML, so plain requests can't see it. We render with Selenium,
let it hydrate, then read the ItemList straight out of the DOM. Price lives in
offers.price (clean), so no per-product availability calls are needed.

Listing pages are URL-paginated: ?page=1..N, 12 products per page.

Run on the PC:   py plaisio_plp.py
                 py plaisio_plp.py --show     (watch the browser, non-headless)
Output:          plaisio_plp.xlsx   (close it in Excel before re-running!)
"""

import re
import sys
import time
import json

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = "https://www.plaisio.gr"

# Category list-paths, all confirmed from Plaisio's own subcategory nav.
CATEGORIES = {
    "fridge_freezer": "/list/megales-oikiakes-siskeves/psigeia-sintirisi/psigeiokatapsiktes",
    "two_door":       "/list/megales-oikiakes-siskeves/psigeia-sintirisi/diporta-psigeia",
    "multi_door":     "/list/megales-oikiakes-siskeves/psigeia-sintirisi/ntoulapes-multi-door",
    "freezer":        "/list/megales-oikiakes-siskeves/psigeia-sintirisi/katapsiktes",
    "mini_bar":       "/list/megales-oikiakes-siskeves/psigeia-sintirisi/mikra-psigeia-mini-bars",
    "preservation":   "/list/megales-oikiakes-siskeves/psigeia-sintirisi/siskeues-sintirisis",
}

# Laundry department (own list-paths; note dryers sit under a different parent).
LAUNDRY = {
    "washing_machine": "/list/megales-oikiakes-siskeves/plisi-stegnoma-rouxon/plintiria-rouxon",
    "washer_dryer":    "/list/megales-oikiakes-siskeves/plisi-stegnoma-rouxon/plintiria-stegnotiria",
    "dryer":           "/list/megales-oikiakes-siskeves/stegnoma-rouxon/stegnotiria",
}
LAUNDRY_COLS = ["site", "category", "department", "sku_id", "brand", "name",
                "price", "availability", "energy", "wash_kg", "dry_kg", "rpm",
                "programs", "heat_pump", "condenser", "loader", "image", "url"]

# load type isn't in the listing item -> crawl the two filtered list paths.
_PWM = ("/list/megales-oikiakes-siskeves/plisi-stegnoma-rouxon/plintiria-rouxon/"
        "typos-siskevis-plintiriou--")
LOADER_FILTERS = {"front": _PWM + "emprosthias-fortosis", "top": _PWM + "ano-fortosis"}

PER_PAGE = 12          # Plaisio serves 12 products per page
MAX_PAGES = 40         # safety cap (173 / 12 ~= 15 pages)

# JS that pulls the ld+json ItemList out of the hydrated DOM and returns
# its product objects (+ the reported total) to Python.
EXTRACT_JS = r"""
const blocks = [...document.querySelectorAll('script[type="application/ld+json"]')]
  .map(s => { try { return JSON.parse(s.textContent); } catch (e) { return null; } })
  .filter(Boolean);
const il = blocks.find(j => j && j["@type"] === "ItemList");
if (!il) return {total: null, items: [], specMap: {}};
const items = (il.itemListElement || []).map(e => e.item || e);
// landscape layout renders a labelled spec line inside each product card;
// map sku (from the product URL) -> the card's text so Python can mine it.
const specMap = {};
document.querySelectorAll('a[href*="/product/"]').forEach(a => {
  const href = a.getAttribute('href') || "";
  const m = href.match(/_(\d{4,})(?:$|[?#/])/);
  if (!m) return;
  const sku = m[1];
  if (specMap[sku]) return;
  let el = a;
  for (let i = 0; i < 8 && el; i++) {
    const t = el.innerText || "";
    if (/Ενεργειακ[ήη]\s+κλάση/i.test(t)) { specMap[sku] = t; break; }
    el = el.parentElement;
  }
});
return {total: il.numberOfItems || null, items: items, specMap: specMap};
"""


def make_chrome(headless=True):
    o = Options()
    if headless:
        o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--lang=el-GR")
    o.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
    try:
        return webdriver.Chrome(options=o)
    except Exception:
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                                options=o)


def accept_cookies(driver):
    for by, sel in [
        (By.ID, "onetrust-accept-btn-handler"),
        (By.XPATH, "//button[contains(., 'Αποδοχή')]"),
        (By.XPATH, "//button[contains(., 'Συμφωνώ')]"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
    ]:
        try:
            WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((by, sel))).click()
            return
        except Exception:
            continue


def wait_for_itemlist(driver, timeout=12):
    """Poll until the hydrated ld+json ItemList shows up (or timeout)."""
    end = time.time() + timeout
    while time.time() < end:
        data = driver.execute_script(EXTRACT_JS)
        if data and data.get("items"):
            return data
        time.sleep(0.5)
    return {"total": None, "items": []}


def price_to_float(v):
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# light spec mining from name/description (Plaisio listing has no structured
# specs; capacity & cooling are often embedded in text)
_LT = re.compile(r"(\d{2,4})\s*[Ll]t\b")
_COOLING = [
    ("Full No Frost", "Full No Frost"),
    ("Total No Frost", "Total No Frost"),
    ("No Frost", "No Frost"),
    ("Low Frost", "Low Frost"),
]


_DIMS3 = re.compile(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*[xX×]\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*[xX×]\s*(\d{2,3}(?:[.,]\d{1,2})?)")


def mine_specs(name, desc):
    text = f"{name} {desc}"
    cap = ""
    m = _LT.search(text)
    if m:
        cap = m.group(1) + " Lt"
    cooling = ""
    for needle, label in _COOLING:
        if needle.lower() in text.lower():
            cooling = label
            break
    # dimensions often appear in the description as ΥxΠxΒ; keep the raw triple
    dims = ""
    dm = _DIMS3.search(text)
    if dm:
        dims = dm.group(0).replace(",", ".")
    return cap, cooling, dims


# ---- laundry spec mining (text-based) ----
_KG = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)", re.I)
_WD_SLASH = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)?\s*/\s*(\d{1,2}(?:[.,]\d)?)\s*(?:kg|κιλ)", re.I)
_RPM = re.compile(r"((?:\d[.,])?\d{3,4})\s*(?:στροφ|rpm|σ\.?α\.?λ|σαλ)", re.I)
_PROG = re.compile(r"(\d{1,2})\s*προγρ", re.I)
# energy class: Greek copy often uses the Greek look-alike letters (Α/Β/Ε) which
# are different code points from Latin A/B/E -> accept both, normalise to Latin.
_EN = re.compile(r"ενεργειακ\w*\s*(?:κλάσ\w*)?\s*[:\-]?\s*([A-GΑΒΕΖ]\+{0,3})", re.I)
_GR2LAT = {"Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z"}


def _energy_of(m):
    if not m:
        return ""
    raw = m.group(1)
    head = raw[0].upper()
    return _GR2LAT.get(head, head) + raw[1:]


def _fmtkg(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def mine_laundry(name, desc, cat_name):
    text = f"{name} {desc}"
    low = text.lower()
    kgs = [float(m.group(1).replace(",", ".")) for m in _KG.finditer(text)]
    kgs = [k for k in kgs if 3 <= k <= 20]
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
    else:
        if kgs:
            wash_kg = _fmtkg(max(kgs))
    rpm = _RPM.search(text)
    pr = _PROG.search(text)
    return {
        "wash_kg": wash_kg, "dry_kg": dry_kg,
        "rpm": re.sub(r"\D", "", rpm.group(1)) if rpm else "",
        "energy": _energy_of(_EN.search(text)),
        "programs": pr.group(1) if pr else "",
        "heat_pump": "Διαθέτει" if "αντλία θερμότητας" in low else "",
        "condenser": "Διαθέτει" if "συμπύκνωσ" in low else "",
    }


# landscape layout exposes a labelled line, e.g.:
#   "Χωρητικότητα: 8 kg, Στροφές (ταχύτητα στυψίματος): 1.400 rpm,
#    Ενεργειακή κλάση: A, Αριθμός προγραμμάτων: 15"
_LS_KG   = re.compile(r"Χωρητικότητα[^:]*:\s*(\d{1,2}(?:[.,]\d)?)\s*kg", re.I)
_LS_DRY  = re.compile(r"Χωρητικότητα\s+στεγν\w*[^:]*:\s*(\d{1,2}(?:[.,]\d)?)\s*kg", re.I)
_LS_RPM  = re.compile(r"Στροφές[^:]*:\s*([\d.,]+)\s*rpm", re.I)
_LS_EN   = re.compile(r"Ενεργειακ[ήη]\s+κλάση\s*:\s*([A-GΑΒΕΖ]\+{0,3})", re.I)
_LS_PROG = re.compile(r"Αριθμός\s+προγραμμάτων\s*:\s*(\d{1,2})", re.I)


def mine_landscape(text, cat_name):
    if not text:
        return {}
    low = text.lower()
    kg_all = [m.group(1).replace(",", ".") for m in _LS_KG.finditer(text)]
    dry = _LS_DRY.search(text)
    wash_kg = dry_kg = ""
    if cat_name == "dryer":
        if kg_all:
            dry_kg = _fmtkg(float(kg_all[0]))
    elif cat_name == "washer_dryer":
        if len(kg_all) >= 2:
            wash_kg, dry_kg = _fmtkg(float(kg_all[0])), _fmtkg(float(kg_all[1]))
        elif kg_all:
            wash_kg = _fmtkg(float(kg_all[0]))
        if dry and not dry_kg:
            dry_kg = _fmtkg(float(dry.group(1).replace(",", ".")))
    else:
        if kg_all:
            wash_kg = _fmtkg(float(kg_all[0]))
    rpm = _LS_RPM.search(text)
    pr = _LS_PROG.search(text)
    return {
        "wash_kg": wash_kg, "dry_kg": dry_kg,
        "rpm": re.sub(r"\D", "", rpm.group(1)) if rpm else "",
        "energy": _energy_of(_LS_EN.search(text)),
        "programs": pr.group(1) if pr else "",
        "heat_pump": "Διαθέτει" if "αντλία θερμότητας" in low else "",
        "condenser": "Διαθέτει" if "συμπύκνωσ" in low else "",
    }


def parse_items(items, cat_name, dept="cooling", spec_map=None):
    spec_map = spec_map or {}
    out = []
    for it in items:
        if not it:
            continue
        offers = it.get("offers") or {}
        brand = it.get("brand") or {}
        if isinstance(brand, dict):
            brand = brand.get("name", "")
        name = it.get("name", "")
        desc = it.get("description", "") or ""
        img = it.get("image") or ""
        if isinstance(img, list):
            img = (img[0] if img else "") or ""
        if isinstance(img, dict):
            img = img.get("url", "") or img.get("contentUrl", "")
        sku = str(it.get("sku", ""))
        # identity: schema.org Product carries gtin*/mpn directly
        ean = ""
        for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8"):
            v = it.get(k)
            if v:
                ean = re.sub(r"\D", "", str(v))
                break
        mpn = str(it.get("mpn", "") or "").strip()
        base = {
            "site": "plaisio",
            "category": cat_name,
            "sku_id": sku,
            "ean": ean,
            "mpn": mpn,
            "brand": brand,
            "name": name,
            "price": price_to_float(offers.get("price")),
            "availability": (offers.get("availability", "") or "").split("/")[-1],
            "image": img,
            "url": it.get("url", ""),
        }
        if dept == "laundry":
            base["department"] = "laundry"
            la = mine_landscape(spec_map.get(sku, ""), cat_name)   # labelled, authoritative
            tla = mine_laundry(name, desc, cat_name)               # name/desc fallback
            for k in ("wash_kg", "dry_kg", "rpm", "energy", "programs", "heat_pump", "condenser"):
                base[k] = la.get(k) or tla.get(k, "")
        else:
            cap, cooling, dims = mine_specs(name, desc)
            base.update({"capacity": cap, "cooling": cooling, "dimensions": dims})
        out.append(base)
    return out


def loader_map(driver, debug=False):
    """{sku_id: 'front'|'top'} by crawling the two load-type filter listings."""
    out = {}
    for tag, path in LOADER_FILTERS.items():
        try:
            rows, _ = collect_category(driver, path, "wm_" + tag, debug=False, dept="laundry")
        except Exception as e:
            print(f"  ! plaisio loader '{tag}' failed: {e}"); rows = []
        for r in rows:
            if r.get("sku_id"):
                out[str(r["sku_id"])] = tag
        print(f"  loader[{tag}]: {len(rows)} washing machines")
    return out


def attach_loaders(rows, driver, debug=False):
    """Tag washing_machine rows by loader (washer_dryer defaults to front)."""
    lm = loader_map(driver) if any(r.get("category") == "washing_machine" for r in rows) else {}
    for r in rows:
        cat = r.get("category")
        r["loader"] = (lm.get(str(r.get("sku_id", "")), "") if cat == "washing_machine"
                       else "front" if cat == "washer_dryer" else "")
    return rows


def collect_category(driver, cat_path, cat_name, debug=True, dept="cooling"):
    merged = {}
    total = None
    base_url = BASE + cat_path

    for page in range(1, MAX_PAGES + 1):
        lay = "&layout=landscape" if dept == "laundry" else ""
        url = f"{base_url}?page={page}{lay}"
        driver.get(url)
        if page == 1:
            accept_cookies(driver)
        data = wait_for_itemlist(driver)
        if total is None and data.get("total"):
            total = data["total"]
        rows = parse_items(data.get("items", []), cat_name, dept=dept, spec_map=data.get("specMap"))
        first_sku = rows[0]["sku_id"] if rows else "-"
        new = [r for r in rows if r["sku_id"] not in merged]
        for r in new:
            merged[r["sku_id"]] = r
        if debug:
            print(f"  page {page}: {len(rows)} items (first sku {first_sku}), "
                  f"+{len(new)} new -> {len(merged)}/{total or '?'}")
        if not rows:
            print("    empty page - stopping.")
            break
        if not new:
            print("    no new products (page repeated) - stopping.")
            break
        if total and len(merged) >= total:
            print("    full category captured - stopping.")
            break

    return list(merged.values()), total


def write_excel(rows, path="plaisio_plp.xlsx", cols=None, title="plaisio"):
    if cols is None:
        cols = ["site", "category", "sku_id", "brand", "name", "price",
                "availability", "capacity", "cooling", "image", "url"]
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


def dump_item(driver, cat_name="washing_machine"):
    """Diagnostic: print one raw ld+json item so we can see whether Plaisio
    carries any structured spec field (e.g. additionalProperty) we could mine
    for energy, the way Kotsovolos exposes its attributes[] array."""
    import json
    path = LAUNDRY.get(cat_name) or CATEGORIES.get(cat_name)
    if not path:
        print(f"  unknown type '{cat_name}'. Try one of: {list(LAUNDRY)} or {list(CATEGORIES)}")
        return
    driver.get(BASE + path)
    accept_cookies(driver)
    data = wait_for_itemlist(driver)
    items = data.get("items", []) or []
    if not items:
        print("  no items returned"); return
    it = items[0]
    print(f"\n=== raw Plaisio item: {it.get('name','?')} ({cat_name}) ===")
    print("KEYS:", list(it.keys()))
    ap = it.get("additionalProperty")
    if ap:
        print("\n-- additionalProperty (this is what we want!) --")
        print(json.dumps(ap, ensure_ascii=False, indent=1)[:3000])
    else:
        print("\n-- no additionalProperty on the listing item --")
        print("   (energy will stay text-mined for Plaisio unless it's in the PDP)")
    print("\n-- full item JSON (first 3500 chars) --")
    print(json.dumps(it, ensure_ascii=False, indent=1)[:3500])


def main():
    # py plaisio_plp.py                 -> fridges (plaisio_plp.xlsx)
    # py plaisio_plp.py laundry         -> all laundry (plaisio_laundry.xlsx)
    # py plaisio_plp.py dump [type]     -> print one raw item (diagnostic)
    # py plaisio_plp.py washing_machine -> one laundry type   (add --show to watch)
    raw = [a for a in sys.argv[1:] if not a.startswith("--")]
    headless = "--show" not in sys.argv
    if raw and raw[0] == "dump":
        cat = raw[1] if len(raw) > 1 else "washing_machine"
        print("Starting Chrome..." + (" (visible)" if not headless else ""))
        d = make_chrome(headless=headless)
        try:
            dump_item(d, cat)
        finally:
            d.quit()
        return
    targets = list(LAUNDRY) if raw == ["laundry"] else (raw if raw else list(CATEGORIES))
    laundry_t = [t for t in targets if t in LAUNDRY]
    fridge_t = [t for t in targets if t in CATEGORIES]
    unknown = [t for t in targets if t not in CATEGORIES and t not in LAUNDRY]
    if unknown:
        print(f"Unknown {unknown}. Fridge:{list(CATEGORIES)} Laundry:{list(LAUNDRY)} (or 'laundry')")
        sys.exit(1)

    print("Starting Chrome..." + (" (visible)" if not headless else ""))
    driver = make_chrome(headless=headless)
    try:
        if fridge_t:
            _run(driver, fridge_t, CATEGORIES, "cooling", "plaisio_plp.xlsx", None, "plaisio")
        if laundry_t:
            _run(driver, laundry_t, LAUNDRY, "laundry", "plaisio_laundry.xlsx",
                 LAUNDRY_COLS, "plaisio_laundry")
    finally:
        driver.quit()


def _run(driver, targets, source, dept, out_path, cols, title):
    all_rows, per_cat = {}, {}
    for cat_name in targets:
        print(f"\n=== {cat_name}  ({source[cat_name]})  [{dept}] ===")
        rows, total = collect_category(driver, source[cat_name], cat_name, dept=dept)
        for r in rows:
            all_rows.setdefault(r["sku_id"], r)
        per_cat[cat_name] = (len(rows), total)
    rows = list(all_rows.values())
    if dept == "laundry":
        attach_loaders(rows, driver)
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
        wm = [r for r in rows if r.get("category") == "washing_machine"]
        fr = sum(1 for r in wm if r.get("loader") == "front")
        tp = sum(1 for r in wm if r.get("loader") == "top")
        print(f"  pillars: wash_kg {fl('wash_kg')}, dry_kg {fl('dry_kg')}, rpm {fl('rpm')}, "
              f"energy {fl('energy')}, programs {fl('programs')} /{len(rows)}")
        print(f"  loader: front {fr}, top {tp}, untagged {len(wm)-fr-tp} /{len(wm)} washing machines")
    write_excel(rows, path=out_path, cols=cols, title=title)


if __name__ == "__main__":
    main()