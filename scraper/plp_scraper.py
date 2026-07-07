#!/usr/bin/env python3
"""
PLP (category-listing) price+spec scraper — INSPECTION BUILD.

Why this exists: visiting one PDP per product (~2000 page loads) is slow and
crash-prone. The category LISTING pages expose price + (for Kotsovolos) full
specs for ~90 products at once. This scraper reads those listing pages and is
MUCH faster.

SAFETY: this build writes an Excel file for you to eyeball. It does NOT push to
the backend / live database. Once the Excel looks correct, we wire it into the
real runner.

Usage (on your PC, where the sites are reachable):
    py plp_scraper.py                 # all 3 sites, fridge_freezer category
    py plp_scraper.py --site public   # one site
    py plp_scraper.py --out test.xlsx # custom output filename

Requires: requests, beautifulsoup4, pandas, openpyxl, selenium (Kotsovolos only)
"""
import sys, re, json, time, argparse, unicodedata

# ----------------------------------------------------------------------------
# Category listing URLs (fridge_freezer to start; same pattern extends to others)
# ----------------------------------------------------------------------------
LISTINGS = {
    "public":     "https://www.public.gr/cat/oikiakes-syskeyes/psygeia/psygeiokatapsiktes?r=90",
    "plaisio":    "https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/psigeiokatapsiktes",
    "kotsovolos": "https://www.kotsovolos.gr/household-appliances/fridges/fridge-freezers",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _strip(s):
    """Accent-insensitive uppercase for robust Greek matching."""
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").upper())
                   if unicodedata.category(c) != "Mn")


def price_to_float(text):
    if text is None: return None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s: return None
    if "," in s: s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        last = s.split(".")[-1]
        if not (s.count(".") == 1 and len(last) in (1, 2)):
            s = s.replace(".", "")
    try: return float(s)
    except Exception: return None


# ----------------------------------------------------------------------------
# PUBLIC — clean schema.org ItemList JSON embedded in the page
# ----------------------------------------------------------------------------
def parse_public_itemlist(json_text):
    """Parse a Public ItemList JSON blob -> list of product dicts."""
    rows = []
    data = json.loads(json_text)
    for el in data.get("itemListElement", []):
        item = el.get("item") or {}
        if item.get("@type") != "Product":
            continue
        offers = item.get("offers") or {}
        if isinstance(offers, list): offers = offers[0] if offers else {}
        price = offers.get("price")
        rating = (item.get("aggregateRating") or {}).get("ratingValue")
        reviews = (item.get("aggregateRating") or {}).get("reviewCount")
        url = item.get("url") or ""
        if url.startswith("/"): url = "https://www.public.gr" + url
        rows.append({
            "site": "public",
            "name": item.get("name"),
            "price": price_to_float(str(price)) if price is not None else None,
            "sku": item.get("sku") or _sku_from_url(url),
            "url": url,
            "energy": None, "capacity": None, "cooling": None, "color": None,
            "rating": rating, "reviews": reviews,
        })
    return rows


# ----------------------------------------------------------------------------
# PLAISIO — also a clean schema.org ItemList JSON
# ----------------------------------------------------------------------------
def parse_plaisio_itemlist(json_text):
    rows = []
    data = json.loads(json_text)
    for el in data.get("itemListElement", []):
        item = el.get("item") or {}
        if item.get("@type") != "Product":
            continue
        offers = item.get("offers") or {}
        if isinstance(offers, list): offers = offers[0] if offers else {}
        price = offers.get("price")
        # capacity is often stated in the description ("χωρητικότητας 363Lt")
        cap = None
        m = re.search(r"(\d{2,3})\s*lt", (item.get("description") or ""), re.I)
        if m: cap = int(m.group(1))
        rows.append({
            "site": "plaisio",
            "name": item.get("name"),
            "price": price_to_float(str(price)) if price is not None else None,
            "sku": item.get("sku"),
            "url": item.get("url"),
            "energy": None, "capacity": cap, "cooling": None, "color": None,
            "rating": None, "reviews": None,
        })
    return rows


# ----------------------------------------------------------------------------
# KOTSOVOLOS — rich cards: data-cnstrc-* attributes + label:value spec rows
# ----------------------------------------------------------------------------
# Greek spec labels -> our normalized fields (accent-stripped, uppercase keys)
_KOTSO_SPECS = {
    "ΕΝΕΡΓΕΙΑΚΗ ΚΛΑΣΗ": "energy",
    "ΚΑΘΑΡΗ ΣΥΝΟΛΙΚΗ ΧΩΡΗΤΙΚΟΤΗΤΑ": "capacity",
    "ΤΥΠΟΣ ΨΥΞΗΣ": "cooling",
    "ΧΡΩΜΑ": "color",
    "ΔΙΑΣΤΑΣΗ": "dims",
}

def parse_kotsovolos_cards(html):
    """Parse Kotsovolos listing HTML -> product dicts. Uses the stable
    data-cnstrc-* attributes for id/name/price, and reads spec rows by Greek
    label text (NOT by the volatile auto-generated CSS classes)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select("[data-cnstrc-item-id]"):
        name = card.get("data-cnstrc-item-name")
        price = price_to_float(card.get("data-cnstrc-item-price"))
        sku = card.get("data-cnstrc-item-id")
        link = card.select_one('a[href*="/household-appliances"]')
        url = link.get("href") if link else None
        rec = {"site": "kotsovolos", "name": name, "price": price, "sku": sku,
               "url": url, "energy": None, "capacity": None, "cooling": None,
               "color": None, "rating": None, "reviews": None}
        # spec rows: <span>label:</span><span>value</span>
        for row in card.select("div.flex-row"):
            spans = row.find_all("span")
            if len(spans) < 2: continue
            label = _strip(spans[0].get_text()).replace(":", "").strip()
            label = re.sub(r"\s*\(.*?\)\s*", "", label).strip()  # drop "(cm)","(lt)"
            value = spans[1].get_text().strip()
            for key_lbl, field in _KOTSO_SPECS.items():
                if label.startswith(key_lbl):
                    if field == "capacity":
                        mm = re.search(r"\d+", value); rec["capacity"] = int(mm.group()) if mm else None
                    else:
                        rec[field] = value
        rows.append(rec)
    return rows


def _sku_from_url(url):
    m = re.search(r"/(\d{5,})(?:[/?]|$)", url or "")
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# Fetchers — Public/Plaisio via requests (JSON in page); Kotsovolos via browser
# ----------------------------------------------------------------------------
def _extract_itemlist_json(html, debug=False):
    """Find the schema.org ItemList <script> block in page HTML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all("script", {"type": "application/ld+json"})
    if debug:
        types = []
        for tag in blocks:
            txt = tag.string or ""
            m = re.search(r'"@type"\s*:\s*"([^"]+)"', txt)
            types.append(m.group(1) if m else "?")
        log(f"    [debug] {len(blocks)} ld+json blocks, types: {types}")
    for tag in blocks:
        txt = tag.string or ""
        if '"ItemList"' in txt and "itemListElement" in txt:
            return txt
    # Fallback: the ItemList JSON may be embedded somewhere other than a
    # <script type=application/ld+json> tag (e.g. a Next.js data blob). Scan
    # the raw HTML for an ItemList object and extract it by brace-matching.
    idx = html.find('"@type":"ItemList"')
    if idx == -1: idx = html.find('"@type": "ItemList"')
    if idx != -1:
        start = html.rfind("{", 0, idx)
        if start != -1:
            depth = 0
            for i in range(start, len(html)):
                if html[i] == "{": depth += 1
                elif html[i] == "}":
                    depth -= 1
                    if depth == 0:
                        cand = html[start:i+1]
                        if "itemListElement" in cand:
                            if debug: log("    [debug] found ItemList via raw-HTML brace scan")
                            return cand
                        break
    return None


def _browser_html(url, wait_selector=None, scroll=False):
    """Load a JS-rendered page in Chrome and return the final HTML."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    o = Options()
    o.add_argument("--window-size=1500,1000")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument(f"user-agent={UA}")
    d = webdriver.Chrome(options=o)
    try:
        d.get(url)
        if wait_selector:
            try: WebDriverWait(d, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
            except Exception: pass
        else:
            time.sleep(3)
        if scroll:
            last = 0
            for _ in range(30):
                d.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.0)
                n = len(d.find_elements(By.CSS_SELECTOR, "[data-cnstrc-item-id]"))
                if n == last and n > 0: break
                last = n
        return d.page_source
    finally:
        d.quit()


def _browser_html_for_itemlist(url):
    """Load a JS-rendered page and wait specifically for the ItemList JSON to
    appear (not just any ld+json). Polls up to ~24s, scrolling to trigger lazy
    rendering, then returns the HTML once the ItemList is present."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options()
    o.add_argument("--window-size=1500,1000")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument(f"user-agent={UA}")
    d = webdriver.Chrome(options=o)
    try:
        d.get(url)
        html = ""
        for attempt in range(12):           # ~24s max
            time.sleep(2)
            d.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            html = d.page_source
            if "ItemList" in html and "itemListElement" in html:
                log(f"    ItemList JSON appeared after ~{(attempt+1)*2}s")
                return html
        log("    ItemList JSON never appeared in ~24s; returning last HTML for diagnostics")
        return html
    finally:
        d.quit()


def _add_param(url, key, val):
    """Add or replace a query param on a URL."""
    if f"{key}=" in url:
        return re.sub(rf"{key}=\d+", f"{key}={val}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={val}"


def fetch_public(url, max_pages=12):
    """Public shows the ItemList JSON for the requested page size (?r=N) and
    paginates the rest via a 'load more' that doesn't update that JSON. So we
    FIRST try a large page size to grab everything in one clean JSON payload;
    if that returns fewer than the category total, we fall back to walking
    ?lmp pages and dedupe by SKU."""
    seen, rows = set(), []
    # Attempt 1: ask for a large page so the ItemList JSON contains all products
    big = _add_param(url, "r", 250)
    html = _browser_html_for_itemlist(big)
    js = _extract_itemlist_json(html, debug=True)
    first = parse_public_itemlist(js) if js else []
    for r in first:
        k = r.get("sku") or r.get("url")
        if k not in seen: seen.add(k); rows.append(r)
    total = _public_total(html)
    log(f"    public ?r=250: {len(rows)} products (category total ~{total or '?'})")
    if total and len(rows) >= total - 2:     # got essentially everything
        return rows
    # Attempt 2 (fallback): walk lmp pages, dedupe
    base = _add_param(url, "r", 90)
    for page in range(1, max_pages + 1):
        page_url = _add_param(base, "lmp", page)
        html = _browser_html_for_itemlist(page_url)
        js = _extract_itemlist_json(html)
        page_rows = parse_public_itemlist(js) if js else []
        new = [r for r in page_rows if (r.get("sku") or r.get("url")) not in seen]
        for r in new: seen.add(r.get("sku") or r.get("url"))
        log(f"    public lmp={page}: {len(page_rows)} parsed, {len(new)} new (total {len(rows)+len(new)})")
        if not new: break
        rows.extend(new)
    return rows


def _public_total(html):
    """Extract the category product count, e.g. '235 προϊόντα' or numberOfItems."""
    m = re.search(r'"numberOfItems"\s*:\s*(\d+)', html)
    if m: return int(m.group(1))
    m = re.search(r"(\d+)\s*προ\w*ντα", html)
    if m: return int(m.group(1))
    return None


def fetch_plaisio(url):
    html = _browser_html_for_itemlist(url)
    js = _extract_itemlist_json(html, debug=True)
    return parse_plaisio_itemlist(js) if js else []


def fetch_kotsovolos(url):
    """Kotsovolos: render + scroll to load all lazy cards, then parse."""
    html = _browser_html(url, wait_selector="[data-cnstrc-item-id]", scroll=True)
    return parse_kotsovolos_cards(html)


FETCHERS = {"public": fetch_public, "plaisio": fetch_plaisio, "kotsovolos": fetch_kotsovolos}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", choices=list(LISTINGS.keys()), help="only this site")
    ap.add_argument("--out", default="plp_test.xlsx", help="output Excel filename")
    args = ap.parse_args()

    sites = [args.site] if args.site else list(LISTINGS.keys())
    all_rows = []
    for s in sites:
        log(f"=== {s} : fetching listing ===")
        try:
            rows = FETCHERS[s](LISTINGS[s])
            log(f"  {s}: {len(rows)} products parsed")
            # quick sanity: how many have a plausible price?
            priced = [r for r in rows if r["price"] and r["price"] >= 40]
            log(f"  {s}: {len(priced)} with plausible price (>=40)")
            all_rows.extend(rows)
        except Exception as e:
            log(f"  {s}: ERROR {e}")

    if not all_rows:
        log("No rows parsed. (On your PC the sites are reachable; in a sandbox they block.)")
        return

    import pandas as pd
    df = pd.DataFrame(all_rows, columns=["site", "name", "price", "energy",
                                         "capacity", "cooling", "color",
                                         "rating", "reviews", "sku", "url"])
    df.to_excel(args.out, index=False)
    log(f"Wrote {len(df)} rows -> {args.out}")
    log("Open it and check: prices look real? specs populated for Kotsovolos?")


if __name__ == "__main__":
    main()
