#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
public_plp.py  -  Pricedge: Public.gr category fetcher (INSPECTION build)

Pulls an ENTIRE Public.gr category via the /category/ JSON API using its
cursor pagination (loi / lov / lsv). Clean canonical prices, full top-specs.
Writes an Excel for inspection. Does NOT touch the live DB.

Run on the PC:   py public_plp.py
Output:          public_plp.xlsx   (close it in Excel before re-running!)

Endpoint shape (discovered):
  https://www.public.gr/category/
    ?s=/cat/oikiakes-syskeyes/psygeia/psygeiokatapsiktes/   (category path)
    &p=2                                                     (page number)
    &loi=<last product id from prev page>                    (cursor)
    &lsv=2                                                    (sort version)
    &lov=<last sort/bestSeller value from prev page>          (cursor)
    &getFilters=false
    &locale=el
Each response carries totalCount, products[], and the next loi/lov/lsv.
"""

import sys
import time
import json
import requests

BASE = "https://www.public.gr/category/"

# Category paths (the "s" param). All slugs confirmed from Public's own
# category links. Add more here later (one line each).
CATEGORIES = {
    "fridge_freezer": "/cat/oikiakes-syskeyes/psygeia/psygeiokatapsiktes/",
    "two_door":       "/cat/oikiakes-syskeyes/psygeia/psygeia-diporta/",
    "freezer":        "/cat/oikiakes-syskeyes/psygeia/katapsiktes/",
    "column_larder":  "/cat/oikiakes-syskeyes/psygeia/psygeia-ntoulapes/",
    "mini_bar":       "/cat/oikiakes-syskeyes/psygeia/mini-bars/",
    "wine_cooler":    "/cat/oikiakes-syskeyes/psygeia/sintirites-krasiwn/",
    "built_in":       "/cat/oikiakes-syskeyes/entoixizomenes-suskeues/entixoizomena-psygeia/",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "Referer": "https://www.public.gr/",
    "X-Requested-With": "XMLHttpRequest",
}

# Map Public's Greek topSpec labels -> our column keys
SPEC_MAP = {
    "ενεργειακη κλαση": "energy",
    "συνολικη καθαρη χωρητικοτητα": "capacity",
    "τυπος ψυξης": "cooling",
    "διαστασεις": "dimensions",          # "Διαστάσεις (ΠxΒxΥ)"
    "βαρος": "weight",
    "εγγυηση": "warranty",
    "καθαρη χωρητικοτητα συντηρησης": "fridge_lt",
    "καθαρη χωρητικοτητα καταψυξης": "freezer_lt",
    "χρωμα": "color",
}

# ---- LAUNDRY (separate department; kept out of the fridge CATEGORIES so the
# ---- live --plp push is unaffected until the frontend department view ships) ----
LAUNDRY = {
    "washing_machine": "/cat/oikiakes-syskeyes/plysimo-stegnwma/plyntiria-rouxwn/",
    "washer_dryer":    "/cat/oikiakes-syskeyes/plysimo-stegnwma/plyntiria-stegnwtiria/",
    "dryer":           "/cat/oikiakes-syskeyes/plysimo-stegnwma/stegnwtiria/",
}

# Public returns labelled topSpecs in the listing, so laundry pillars come
# through EXACTLY (not guessed). Specific energy keys are listed BEFORE the
# generic "ενεργειακη κλαση" so washer-dryers split wash vs dry energy correctly
# (the parser breaks on the first startswith match, in insertion order).
LAUNDRY_SPEC_MAP = {
    "ενεργειακη κλαση πλυσης":      "energy",
    "ενεργειακη κλαση στεγνωματος": "dry_energy",
    "χωρητικοτητα πλυσης":          "wash_kg",
    "χωρητικοτητα στεγνωματος":     "dry_kg",
    "χωρητικοτητα για στεγνωμα":    "dry_kg",
    "στροφες":                      "rpm",
    "αριθμος προγραμματων":         "programs",
    "προγραμματα":                  "programs",
    "λειτουργια προγραμματων ατμου": "steam",
    "αντλια θερμοτητας":            "heat_pump",
    "συστημα συμπυκνωσης":          "condenser",
    "διαστασ":                      "dimensions",
    "ενεργειακη κλαση":             "energy",
}
LAUNDRY_COLS = ["site", "category", "department", "sku_id", "brand", "name",
                "price", "list_price", "energy", "dry_energy", "wash_kg",
                "dry_kg", "rpm", "programs", "steam", "heat_pump",
                "condenser", "loader", "dimensions", "image", "url"]

# Load type (top vs front) isn't in topSpecs -> Public exposes it only as a
# category facet, so we crawl the two filtered listings and tag by sku_id.
_WM = "/cat/oikiakes-syskeyes/plysimo-stegnwma/plyntiria-rouxwn/f/typos-fortwshs:"
LOADER_FILTERS = {"front": _WM + "emprosthias-fortwshs", "top": _WM + "anw-fortwshs"}
_LOADER_CACHE = None


def loader_map(debug=False):
    """{sku_id: 'front'|'top'} from the two load-type filter listings."""
    global _LOADER_CACHE
    if _LOADER_CACHE is not None:
        return _LOADER_CACHE
    out = {}
    probe = requests.Session()
    probe.headers.update(HEADERS)
    for tag, path in LOADER_FILTERS.items():
        # Probe the filter URL once before the multi-sort crawl: when Public
        # renames the facet slug the old path 404s, and without this check
        # every sort pass prints its own error line (7 per filter). One clear
        # warning + skip instead; load-type tags simply stay empty until
        # LOADER_FILTERS is updated with the new slug from public.gr.
        try:
            pr = probe.get(BASE, params={"s": path, "p": 1,
                                         "getFilters": "false", "locale": "el"},
                           timeout=20)
            if pr.status_code == 404:
                print(f"  ! loader filter '{tag}' 404s — Public changed the "
                      f"load-type filter URL; update LOADER_FILTERS in "
                      f"public_plp.py (skipping, loader tags stay empty)")
                continue
        except Exception as e:
            print(f"  ! loader filter '{tag}' probe failed: {str(e)[:60]} — skipping")
            continue
        try:
            rows, _ = collect_all(path, cat_name="wm_" + tag, debug=debug,
                                  spec_map=LAUNDRY_SPEC_MAP, dept="laundry")
        except Exception as e:
            print(f"  ! loader filter '{tag}' failed: {e}"); rows = []
        for r in rows:
            if r.get("sku_id"):
                out[str(r["sku_id"])] = tag
        print(f"  loader[{tag}]: {len(rows)} washing machines")
    _LOADER_CACHE = out
    return out


def attach_loaders(rows, debug=False):
    """Tag washing_machine rows by loader (washer_dryer defaults to front)."""
    lm = loader_map(debug=debug) if any(r.get("category") == "washing_machine" for r in rows) else {}
    for r in rows:
        cat = r.get("category")
        r["loader"] = (lm.get(str(r.get("sku_id", "")), "") if cat == "washing_machine"
                       else "front" if cat == "washer_dryer" else "")
    return rows


def _strip(s):
    """Accent-insensitive, lowercase, trimmed - for robust label matching."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def price_to_float(v):
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def parse_products(data, spec_map=SPEC_MAP, dept="cooling"):
    """Turn one API response into a list of flat product dicts.

    spec_map/dept select fridge vs laundry behaviour: laundry rows carry
    kg/rpm/programs/etc. fields instead of cooling/Lt, and are tagged
    department='laundry' so they never blend into the cooling views."""
    out = []
    for entry in data.get("products", []):
        sku = entry.get("sku", {}) or {}
        price_info = sku.get("priceInfoDto", {}) or {}

        # Public's real price fields (confirmed via `dumpprice`): salePrice is
        # the current price the customer pays; listPrice is the pre-markdown
        # price ONLY when hasSalePrice is set (otherwise listPrice==salePrice);
        # rrpPrice is the manufacturer's suggested retail (informational,
        # sometimes 0). Financing/warranty/recycle live in sku["services"].
        price = price_to_float(price_info.get("salePrice"))
        if price is None:
            price = price_to_float(price_info.get("listPrice"))
        lp = price_to_float(price_info.get("listPrice"))
        on_sale = bool(price_info.get("hasSalePrice"))
        # was-price for promo analytics: the struck listPrice, but only on a
        # genuine markdown — never treat the higher manufacturer RRP as a promo.
        list_price_final = lp if (on_sale and lp is not None and price is not None
                                  and lp > price + 0.5) else None
        rrp_price = price_to_float(price_info.get("rrpPrice"))

        brand = (sku.get("brand", {}) or {}).get("displayName", "")
        url = sku.get("url", "")
        if url and url.startswith("/"):
            url = "https://www.public.gr" + url

        # image: field name varies; try the common shapes, normalise relative URLs
        img = ""
        for k in ("mainImage", "image", "imageUrl", "thumbnail", "thumbnailUrl"):
            v = sku.get(k)
            if isinstance(v, str) and v:
                img = v
                break
            if isinstance(v, dict):
                img = v.get("url", "") or v.get("src", "")
                if img:
                    break
        if not img:
            imgs = sku.get("images") or sku.get("media") or sku.get("gallery") or []
            if isinstance(imgs, list) and imgs:
                first = imgs[0]
                img = first if isinstance(first, str) else (
                    (first.get("url", "") or first.get("src", "")) if isinstance(first, dict) else "")
        if img and img.startswith("/"):
            img = "https://www.public.gr" + img

        # identity fields: try the common shapes Public's sku JSON uses.
        # EAN/GTIN is the strongest cross-retailer key; MPN second.
        ean = ""
        for k in ("barcode", "ean", "gtin", "gtin13", "eanCode"):
            v = sku.get(k)
            if v:
                ean = "".join(ch for ch in str(v) if ch.isdigit())
                break
        mpn = ""
        for k in ("mpn", "manufacturerCode", "manufacturerSku", "modelNumber", "model"):
            v = sku.get(k)
            if v and isinstance(v, str):
                mpn = v.strip()
                break

        # availability: explicit field if the API exposes one; otherwise derive
        # it — a priced row on the purchase listing is buyable, and the
        # "Αγορά μόνο από κατάστημα" ribbon downgrades it to in-store-only.
        avail = ""
        for k in ("stockLevelStatus", "availability", "stockStatus",
                  "inventoryStatus", "availabilityStatus"):
            v = sku.get(k)
            if isinstance(v, dict):
                v = v.get("status") or v.get("value") or v.get("code")
            if v:
                avail = str(v)
                break
        if not avail:
            rb = sku.get("ribbons")
            txts = " ".join(str((x.get("text") or x.get("name") or x) if isinstance(x, dict) else x)
                            for x in rb) if isinstance(rb, list) else ""
            if "ΚΑΤΑΣΤΗΜΑ" in _strip(txts).upper():
                avail = "InStoreOnly"
            elif price is not None:
                avail = "InStock"

        row = {
            "site": "public",
            "category": "",
            "department": dept,
            "sku_id": sku.get("id") or entry.get("id", ""),
            "ean": ean,
            "mpn": mpn,
            "availability": avail,
            "brand": brand,
            "name": sku.get("displayName", ""),
            "price": price,
            "list_price": list_price_final,
            "rrp": rrp_price if (rrp_price and rrp_price > 0) else None,
            "energy": "",
            "capacity": "",
            "cooling": "",
            "dimensions": "",
            "weight": "",
            "warranty": "",
            "fridge_lt": "",
            "freezer_lt": "",
            # laundry fields (stay empty for cooling rows)
            "dry_energy": "",
            "wash_kg": "",
            "dry_kg": "",
            "rpm": "",
            "programs": "",
            "steam": "",
            "heat_pump": "",
            "condenser": "",
            "rating": sku.get("averageRating"),
            "reviews": sku.get("totalReviews"),
            "image": img,
            "url": url,
        }

        for spec in sku.get("topSpecs", []) or []:
            label = _strip(spec.get("displayName", ""))
            vals = spec.get("values") or []
            val = vals[0] if vals else ""
            for key_label, col in spec_map.items():
                if label.startswith(key_label):
                    row[col] = val
                    break

        out.append(row)
    return out


def next_cursor(data):
    """Pull the cursor for the next page out of a response."""
    loi = data.get("loi")
    lov = data.get("lov")
    lsv = data.get("lsv", 2)
    if loi is None or lov is None:
        return None
    return {"loi": str(loi), "lov": str(lov), "lsv": str(lsv)}


def fetch_category(cat_path, ob=None, page_pause=0.6, max_pages=60, debug=True,
                   spec_map=SPEC_MAP, dept="cooling"):
    """Loop the cursor pagination until the whole category is collected.

    ob = sort order ("order by"). None = default best-seller. Other values:
    priceDesc, priceAsc, disc, pop, new, rev. Sorting by price is key: every
    product has a non-null price, so the keyset cursor never dead-ends on the
    null best-seller rank of in-store-only ("Αγορά μόνο από κατάστημα") items.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows = []
    seen_ids = set()
    cursor = None
    total = None
    page = 1

    while page <= max_pages:
        params = {
            "s": cat_path,
            "p": page,
            "getFilters": "false",
            "locale": "el",
        }
        if ob:
            params["ob"] = ob
        if cursor:
            params.update(cursor)

        try:
            r = session.get(BASE, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! page {page} failed: {e}")
            break

        if total is None:
            total = data.get("totalCount")
            if debug:
                print(f"  totalCount = {total}")

        rows = parse_products(data, spec_map=spec_map, dept=dept)
        new = [x for x in rows if x["sku_id"] not in seen_ids]
        for x in new:
            seen_ids.add(x["sku_id"])
        all_rows.extend(new)

        if debug:
            print(f"  page {page}: +{len(new)} new "
                  f"({len(all_rows)}/{total or '?'})")

        # Stop conditions
        if not new:
            if debug:
                print("  no new products - stopping.")
            break
        if total and len(all_rows) >= total:
            if debug:
                print("  reached totalCount - done.")
            break

        cursor = next_cursor(data)
        if not cursor:
            if debug:
                print("  no cursor in response - stopping.")
            break

        page += 1
        time.sleep(page_pause)

    return all_rows, total


def write_excel(rows, path="public_plp.xlsx", cols=None, title="public"):
    if cols is None:
        cols = ["site", "category", "sku_id", "brand", "name", "price",
                "list_price", "energy", "capacity", "cooling", "dimensions",
                "weight", "warranty", "fridge_lt", "freezer_lt", "rating",
                "reviews", "image", "url"]
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed. Run: py -m pip install openpyxl")
        # fall back to CSV so we still get output
        path = path.replace(".xlsx", ".csv")
        import csv
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
    # widen a few columns for readability
    widths = {"B": 15, "D": 14, "E": 52, "F": 9, "H": 8, "I": 12,
              "J": 16, "K": 22, "R": 60}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    try:
        wb.save(path)
    except PermissionError:
        print(f"\n  !! {path} is open in Excel. Close it and re-run.\n")
        sys.exit(1)
    # also drop a CSV companion — opens anywhere (Excel/Notepad/Sheets) and
    # sidesteps any "file format not valid" complaint on the .xlsx
    csv_path = path.replace(".xlsx", ".csv")
    import csv
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    except PermissionError:
        csv_path = "(skipped - open in another app)"
    print(f"Wrote {len(rows)} rows -> {path}  (+ {csv_path})")
    return path


def collect_all(cat_path, cat_name="", debug=True, spec_map=SPEC_MAP, dept="cooling"):
    """Union several sort passes until the whole category is captured.

    Each sort's keyset cursor dead-ends at a different subset (ties / null
    ranks stop the cursor early), so no single sort returns everything. We
    cycle sorts and stop as soon as the merged count reaches totalCount.
    Fast categories close in 2 passes; stubborn ones use more, only as needed.
    """
    # ordered for fastest coverage; None = default best-seller
    SORTS = [(None, "best-seller"), ("priceDesc", "price-desc"),
             ("priceAsc", "price-asc"), ("new", "new"),
             ("pop", "popular"), ("rev", "reviewed"), ("disc", "discount")]

    merged = {}
    total_reported = None

    for ob, label in SORTS:
        if debug:
            print(f"  [pass: {label}{' (ob='+ob+')' if ob else ''}]")
        rows, total = fetch_category(cat_path, ob=ob, debug=debug,
                                     spec_map=spec_map, dept=dept)
        if total and not total_reported:
            total_reported = total
        added = 0
        for r in rows:
            r["category"] = cat_name
            if r["sku_id"] not in merged:
                merged[r["sku_id"]] = r
                added += 1
        if debug:
            print(f"    pass added {added} new -> "
                  f"{len(merged)}/{total_reported or '?'} unique")
        # stop early once the category is fully captured
        if total_reported and len(merged) >= total_reported:
            if debug:
                print("    full category captured - stopping passes.")
            break
        # also stop if a pass adds nothing new AND we've tried >=3 sorts
        # (remaining items may be genuinely unreachable via listing)
        if added == 0 and label not in ("best-seller", "price-desc"):
            # one wasted pass after the core sorts -> likely no more gains
            pass

    return list(merged.values()), total_reported


def _run_targets(targets, source, spec_map, dept, out_path, cols, title):
    """Scrape a set of category keys from one department and write one file."""
    all_rows = {}
    per_cat = {}
    for cat_name in targets:
        print(f"\n=== {cat_name}  ({source[cat_name]})  [{dept}] ===")
        rows, total = collect_all(source[cat_name], cat_name=cat_name,
                                  spec_map=spec_map, dept=dept)
        added = 0
        for r in rows:
            if r["sku_id"] not in all_rows:
                all_rows[r["sku_id"]] = r
                added += 1
        per_cat[cat_name] = len(rows)
        status = "full" if (total and len(rows) >= total) else f"of ~{total}"
        print(f"  -> {cat_name}: {len(rows)} products ({status}); {added} new to set")

    rows = list(all_rows.values())
    if dept == "laundry":
        attach_loaders(rows)
    print("\n" + "=" * 48)
    print(f"TOTAL unique {dept} products: {len(rows)}")
    for cat_name, n in per_cat.items():
        print(f"   {cat_name:16} {n}")

    bad = [r for r in rows if not r["price"] or r["price"] < 35]
    if bad:
        print(f"  WARNING: {len(bad)} products with missing/suspicious price:")
        for r in bad[:10]:
            print(f"    {r['sku_id']}  {r['name'][:40]}  -> {r['price']}")
    else:
        print("  price sanity: all prices present and >= 35 EUR  OK")

    if dept == "laundry":
        filled = lambda k: sum(1 for r in rows if r.get(k))
        wm = [r for r in rows if r.get("category") == "washing_machine"]
        front = sum(1 for r in wm if r.get("loader") == "front")
        top = sum(1 for r in wm if r.get("loader") == "top")
        print(f"  pillars filled: wash_kg {filled('wash_kg')}, rpm {filled('rpm')}, "
              f"energy {filled('energy')}, programs {filled('programs')}, "
              f"dimensions {filled('dimensions')}, dry_kg {filled('dry_kg')}, "
              f"dry_energy {filled('dry_energy')} /{len(rows)}")
        print(f"  loader: front {front}, top {top}, untagged {len(wm)-front-top} /{len(wm)} washing machines")

    my_brands = {"WHIRLPOOL", "INDESIT", "BEKO"}
    mine = [r for r in rows if (r["brand"] or "").upper() in my_brands]
    print(f"  your brands (Whirlpool/Indesit/Beko): {len(mine)}")

    write_excel(rows, path=out_path, cols=cols, title=title)


def dump_sap(key):
    """Diagnostic: print sapHierarchy + virtualCategories + ribbons for several
    products, to find where load type (front/top) is encoded for Public."""
    import requests
    src = LAUNDRY if key in LAUNDRY else CATEGORIES
    if key not in src:
        print(f"Unknown category '{key}'."); return
    sess = requests.Session(); sess.headers.update(HEADERS)
    r = sess.get(BASE, params={"s": src[key], "p": 1, "getFilters": "false",
                               "locale": "el"}, timeout=30)
    prods = r.json().get("products", [])
    if not prods:
        print("no products returned"); return
    n = min(12, len(prods))
    print(f"\n=== sapHierarchy / categories for first {n} '{key}' ===")
    for p in prods[:n]:
        sku = p.get("sku", {})
        name = (sku.get("displayName", "") or "")[:55]
        sap = sku.get("sapHierarchy")
        vc = sku.get("virtualCategories")
        vcs = ""
        if isinstance(vc, list):
            vcs = " | ".join(str((v.get("name") or v.get("url") or v) if isinstance(v, dict) else v)
                             for v in vc)[:140]
        rb = sku.get("ribbons")
        rbs = ""
        if isinstance(rb, list):
            rbs = " | ".join(str((v.get("text") or v.get("name") or v) if isinstance(v, dict) else v)
                             for v in rb)[:100]
        print(f"\n- {name}")
        print(f"   sapHierarchy: {sap!r}")
        if vcs: print(f"   virtualCategories: {vcs}")
        if rbs: print(f"   ribbons: {rbs}")


def dump_price(key, needle=None):
    """Diagnostic: print the raw priceInfoDto + offer flags for Public products.
    `key` may be a category name or 'all' (every cooling category). `needle`:
      None        -> first 8 products in the category
      'offers'    -> products flagged as a super/web offer
      <digits>    -> the product with that exact SKU id (the robust path — we
                     already store the retailer SKU per product; on the Public
                     PDP it's shown as 'ΚΩΔΙΚΟΣ')
      <text>      -> name/code substring, matched ignoring spaces/accents
    Sorts are cycled so any product in a category is reachable (a single sort
    dead-ends on Public's keyset cursor)."""
    import re as _re2
    import requests
    sess = requests.Session(); sess.headers.update(HEADERS)
    want_offers = str(needle).lower() == "offers" if needle else False
    by_id = str(needle).isdigit() if needle else False
    nd = None if (by_id or want_offers or not needle) else _re2.sub(r"[^a-z0-9]", "", _strip(needle))

    def show(sku, cat):
        pi = sku.get("priceInfoDto", {}) or {}
        print(f"\n[{cat}] id={sku.get('id')}  {sku.get('displayName','')[:60]}")
        print("  priceInfoDto:", json.dumps(pi, ensure_ascii=False))
        print("  url:", sku.get("url", ""))
        print(f"  flags: superOffer={sku.get('superOfferFlag')} "
              f"webOffer={sku.get('webOfferFlag')} mirakl={sku.get('mirakl')} "
              f"offerCount={sku.get('offerCount')} ribbons="
              f"{[ (r.get('text') or r.get('name')) if isinstance(r,dict) else r for r in (sku.get('ribbons') or []) ]}")

    if key == "all":
        cats = list(CATEGORIES.items())
    elif key in CATEGORIES or key in LAUNDRY:
        cats = [(key, (LAUNDRY if key in LAUNDRY else CATEGORIES)[key])]
    else:
        print(f"Unknown category '{key}'. Use one of {list(CATEGORIES)}, a laundry key, or 'all'."); return

    seen, shown = set(), 0
    for catname, catpath in cats:
        for ob in (None, "disc", "priceAsc", "priceDesc", "new", "pop"):
            cursor = None
            for page in range(1, 40):
                params = {"s": catpath, "p": page, "getFilters": "false", "locale": "el"}
                if ob:
                    params["ob"] = ob
                if cursor:
                    params.update(cursor)
                try:
                    data = sess.get(BASE, params=params, timeout=30).json()
                except Exception as e:
                    print(f"  ! {e}"); break
                prods = data.get("products", [])
                if not prods:
                    break
                for p in prods:
                    sku = p.get("sku", {})
                    sid = sku.get("id")
                    if sid in seen:
                        continue
                    seen.add(sid)
                    if by_id:
                        if str(sid) == str(needle):
                            show(sku, catname); return
                    elif want_offers:
                        if sku.get("superOfferFlag") or sku.get("webOfferFlag"):
                            show(sku, catname); shown += 1
                            if shown >= 8:
                                return
                    elif nd is not None:
                        if nd in _re2.sub(r"[^a-z0-9]", "", _strip(sku.get("displayName", ""))):
                            show(sku, catname); return
                    else:
                        show(sku, catname); shown += 1
                        if shown >= 8:
                            return
                cursor = next_cursor(data)
                if not cursor:
                    break
    if shown == 0:
        what = ("SKU id " + str(needle)) if by_id else \
               ("super/web-offer product" if want_offers else "product matching " + repr(needle))
        print(f"No {what} found in {key}. If it's on the site, the fast category API "
              f"likely doesn't return it (marketplace/Mirakl or delisted) — that's why "
              f"its price would go stale under --plp.")


def dump_pdp(ident):
    """Fetch a product PDP and show every price it exposes — to find where the
    'Άπαιχτη Τιμή' price (lower than the category API) actually lives. `ident`
    is a SKU id (looked up to its URL via the category API) or a URL/path."""
    import re as _re2
    import requests
    sess = requests.Session(); sess.headers.update(HEADERS)
    url = ident
    if not (ident.startswith("http") or ident.startswith("/product")):
        found = None
        for _, catpath in CATEGORIES.items():
            for ob in (None, "disc"):
                cursor = None
                for page in range(1, 40):
                    params = {"s": catpath, "p": page, "getFilters": "false", "locale": "el"}
                    if ob:
                        params["ob"] = ob
                    if cursor:
                        params.update(cursor)
                    data = sess.get(BASE, params=params, timeout=30).json()
                    prods = data.get("products", [])
                    if not prods:
                        break
                    for p in prods:
                        sku = p.get("sku", {})
                        if str(sku.get("id")) == str(ident):
                            found = sku.get("url")
                            break
                    if found:
                        break
                    cursor = next_cursor(data)
                    if not cursor:
                        break
                if found:
                    break
            if found:
                break
        if not found:
            print(f"SKU {ident} not found in the category API."); return
        url = found
    if url.startswith("/"):
        url = "https://www.public.gr" + url
    print("PDP:", url)
    html = sess.get(url, timeout=40).text
    for m in _re2.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, _re2.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "Product":
                print("  ld+json offers:", json.dumps(item.get("offers"), ensure_ascii=False)[:300])
    hits = {}
    for m in _re2.finditer(r'"([a-zA-Z]*[Pp]rice[a-zA-Z]*)"\s*:\s*"?(\d{2,5}(?:\.\d{1,2})?)"?', html):
        v = float(m.group(2))
        if 50 <= v <= 6000:
            hits.setdefault(m.group(1), set()).add(v)
    print("  price-like JSON keys in the PDP:")
    for k, vs in sorted(hits.items()):
        print(f"    {k}: {sorted(vs)}")
    print("  raw '549' present in HTML:", "549" in html)


def dump_keys(key):
    """Diagnostic: print the first product's available fields + every topSpec
    label, so we can pin spec-label keys exactly instead of guessing."""
    import requests
    src = LAUNDRY if key in LAUNDRY else CATEGORIES
    if key not in src:
        print(f"Unknown category '{key}'. Fridge:{list(CATEGORIES)} Laundry:{list(LAUNDRY)}")
        return
    sess = requests.Session(); sess.headers.update(HEADERS)
    r = sess.get(BASE, params={"s": src[key], "p": 1, "getFilters": "false",
                               "locale": "el"}, timeout=30)
    prods = r.json().get("products", [])
    if not prods:
        print("no products returned"); return
    sku = prods[0].get("sku", {})
    print(f"\n=== first product in '{key}' ===")
    print("name:", sku.get("displayName"))
    print("\nSKU top-level keys:", sorted(sku.keys()))
    print("\ntopSpecs labels (label = value):")
    for s in sku.get("topSpecs", []) or []:
        lbl = s.get("displayName", "")
        val = (s.get("values") or [None])[0]
        mapped = next((c for k, c in LAUNDRY_SPEC_MAP.items() if _strip(lbl).startswith(k)), "—")
        print(f"   {lbl!r} = {val!r}   -> {mapped}")


def main():
    # Usage:
    #   py public_plp.py                  -> all FRIDGE categories  (public_plp.xlsx)
    #   py public_plp.py freezer          -> one fridge category
    #   py public_plp.py laundry          -> all LAUNDRY categories (public_laundry.xlsx)
    #   py public_plp.py washing_machine  -> one laundry category
    #   py public_plp.py dumpkeys dryer   -> print real spec labels for a category
    args = sys.argv[1:]

    if args and args[0] == "dumpkeys":
        dump_keys(args[1] if len(args) > 1 else "washing_machine")
        return

    if args and args[0] == "dumpsap":
        dump_sap(args[1] if len(args) > 1 else "washing_machine")
        return

    if args and args[0] == "dumpprice":
        dump_price(args[1] if len(args) > 1 else "fridge_freezer",
                   args[2] if len(args) > 2 else None)
        return

    if args and args[0] == "dumppdp":
        dump_pdp(args[1] if len(args) > 1 else "2052937")
        return

    if args == ["laundry"]:
        targets = list(LAUNDRY)
    else:
        targets = args if args else list(CATEGORIES)

    laundry_t = [t for t in targets if t in LAUNDRY]
    fridge_t = [t for t in targets if t in CATEGORIES]
    unknown = [t for t in targets if t not in CATEGORIES and t not in LAUNDRY]
    if unknown:
        print(f"Unknown category {unknown}. "
              f"Fridge: {list(CATEGORIES)}  Laundry: {list(LAUNDRY)} (or 'laundry')")
        sys.exit(1)

    if fridge_t:
        _run_targets(fridge_t, CATEGORIES, SPEC_MAP, "cooling",
                     "public_plp.xlsx", None, "public")
    if laundry_t:
        _run_targets(laundry_t, LAUNDRY, LAUNDRY_SPEC_MAP, "laundry",
                     "public_laundry.xlsx", LAUNDRY_COLS, "public_laundry")


if __name__ == "__main__":
    main()