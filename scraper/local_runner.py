"""
Pricedge local runner v3  (self-contained, browser-based)
==========================================================
Runs on YOUR PC. Uses ONE real Chrome window for everything, with the exact
selectors proven in your Jupyter scripts:

  Crawl (collect product URLs by clicking the load-more button):
    Kotsovolos : click "Φόρτωση επόμενων", links via a#insider-product-link
    Plaisio    : click "Επόμενο",          links via /i-kouzina-mou/ ... _<id>
    Public     : click "Δες περισσότερα",   links via a.product__gallery__image

  Scrape (read each product page):
    Kotsovolos : insider-final-price-with-vat / characteristics-accordion
    Plaisio    : div.pdp-price-container__price ... / dl specs
    Public     : div.product__price--xxlarge / <strong>label:</strong> specs

Then pushes everything to Railway; unknown SKUs auto-register.

SETUP (one time)
----------------
  pip install requests selenium webdriver-manager beautifulsoup4
Put this file anywhere; it does NOT need the scrapers/ folder. Set INGEST_TOKEN
below to match Railway. Make sure Google Chrome is installed.

RUN
---
  python local_runner.py                # full crawl + scrape + push (all sites)
  python local_runner.py --site plaisio # one site only
  python local_runner.py --quick        # re-scrape known products (from backend list)
  python local_runner.py --headless     # hide the browser window
"""

import os, re, sys, time, json, argparse, requests
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (TimeoutException, ElementClickInterceptedException,
                                        NoSuchElementException, StaleElementReferenceException)

# Fast catalogue-API fetchers (standalone modules in the same folder). These
# pull each retailer's WHOLE category list with clean prices in one pass via
# the APIs we reverse-engineered (Public /category/ cursor, Kotsovolos
# getProductsByCategory, Plaisio hydrated ld+json ItemList). Used by --plp.
try:
    import public_plp, kotsovolos_plp, plaisio_plp
    _PLP_OK = True
    _PLP_ERR = ""
except Exception as _e:                      # standalone files not present
    _PLP_OK = False
    _PLP_ERR = str(_e)

# -- CONFIG ---------------------------------------------------------------
API_URL = os.getenv("PRICEDGE_API", "https://pricedge-backend-production.up.railway.app")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")   # set via env var; never hard-code the secret here
DELAY = 2          # seconds between product scrapes
PUSH_EVERY = 25    # push to backend after this many products

# How often each subcategory is worth re-scraping. Mainstream high-volume
# segments move fast (prices change daily); niche segments (wine coolers,
# mini bars) barely change, so scraping them daily wastes time. The --due
# mode uses these intervals + a local last-run log to scrape only what's due.
SUBCAT_INTERVAL_DAYS = {
    "fridge_freezer": 1,   # mainstream, high volume — daily
    "top_mount":      1,
    "side_by_side":   1,
    "built_in":       2,
    "freezer":        3,
    "one_door":       3,
    "mini_bar":      14,   # niche — fortnightly
    "wine_cooler":   30,   # niche — monthly
}
DEFAULT_INTERVAL_DAYS = 3
LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json")

# Each category URL is tagged with a canonical subcategory so every product
# inherits a clean, reliable subcategory (the #1 BI breakdown). Retailers
# split their menus slightly differently; these map them to one taxonomy.
# Canonical subcategories: fridge_freezer, top_mount, side_by_side, one_door,
#                          freezer, mini_bar, wine_cooler, built_in
CATEGORIES = {
    "kotsovolos": [
        ("https://www.kotsovolos.gr/household-appliances/fridges/fridge-freezers", "fridge_freezer"),
        ("https://www.kotsovolos.gr/household-appliances/fridges/fridges", "top_mount"),
        ("https://www.kotsovolos.gr/household-appliances/fridges/side-by-side-fridges", "side_by_side"),
        ("https://www.kotsovolos.gr/household-appliances/fridges/freezers", "freezer"),
        ("https://www.kotsovolos.gr/household-appliances/fridges/mini-bars", "mini_bar"),
        ("https://www.kotsovolos.gr/household-appliances/fridges/wine-conservation", "wine_cooler"),
        ("https://www.kotsovolos.gr/household-appliances/built-in/built-in-fridges", "built_in"),
    ],
    "plaisio": [
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/psigeiokatapsiktes", "fridge_freezer"),
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/diporta-psigeia", "top_mount"),
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/ntoulapes-multi-door", "side_by_side"),
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/katapsiktes", "freezer"),
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/mikra-psigeia-mini-bars", "mini_bar"),
        ("https://www.plaisio.gr/list/megales-oikiakes-siskeves/psigeia-sintirisi/siskeues-sintirisis", "wine_cooler"),
    ],
    "public": [
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/psygeiokatapsiktes", "fridge_freezer"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/psygeia-ntoulapes", "side_by_side"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/psygeia-diporta", "top_mount"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/katapsiktes", "freezer"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/mini-bars", "mini_bar"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/psygeia/sintirites-krasiwn", "wine_cooler"),
        ("https://www.public.gr/cat/oikiakes-syskeyes/entoixizomenes-suskeues/entixoizomena-psygeia", "built_in"),
    ],
}

# Maps each standalone fetcher's category key -> the canonical subcategory
# taxonomy used everywhere in the app/backend. Mirrors the tags in CATEGORIES
# above so --plp products land in the same buckets as the crawl+PDP path.
PLP_SUBCAT_MAP = {
    "public": {
        "fridge_freezer": "fridge_freezer", "two_door": "top_mount",
        "freezer": "freezer", "column_larder": "side_by_side",
        "mini_bar": "mini_bar", "wine_cooler": "wine_cooler", "built_in": "built_in",
    },
    "plaisio": {
        "fridge_freezer": "fridge_freezer", "two_door": "top_mount",
        "multi_door": "side_by_side", "freezer": "freezer",
        "mini_bar": "mini_bar", "preservation": "wine_cooler",
    },
    "kotsovolos": {
        "fridge_freezer": "fridge_freezer", "fridges": "top_mount",
        "side_by_side": "side_by_side", "freezer": "freezer",
        "mini_bar": "mini_bar", "wine_cooler": "wine_cooler",
        "built_in": "built_in",
    },
}
# -------------------------------------------------------------------------

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def _start_watchdog(minutes):
    """Fail-safe for automated/scheduled runs. If the run hangs (e.g. a Plaisio
    Selenium page stalls), force-exit after `minutes` so the process can't sit
    powered on for hours. Runs in a daemon Timer thread; 0/None disables it.
    A hard os._exit is used so stuck browser/network threads can't block it —
    the calling .bat then continues to its shutdown step normally."""
    if not minutes or minutes <= 0:
        return None
    import threading
    def _kill():
        log(f"WATCHDOG: max runtime of {minutes} min hit — forcing exit so the "
            f"scheduled run can finish and the PC can shut down.")
        try: sys.stdout.flush()
        except Exception: pass
        os._exit(2)
    t = threading.Timer(minutes * 60, _kill)
    t.daemon = True
    t.start()
    log(f"Watchdog armed: will force-exit after {minutes} min if the run hangs.")
    return t

def _load_last_run():
    """Read the per-subcategory last-run log from disk."""
    try:
        with open(LAST_RUN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_last_run(data):
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠ could not write last_run log: {e}")

def _all_subcats():
    return set(sum([[c[1] for c in v] for v in CATEGORIES.values()], []))

def _due_subcats(now=None):
    """Subcategories due for a scrape, based on each one's interval and when it
    last ran. A subcategory never scraped before is always due."""
    import datetime as _dt
    now = now or _dt.datetime.now()
    last = _load_last_run()
    due = set()
    for sub in _all_subcats():
        interval = SUBCAT_INTERVAL_DAYS.get(sub, DEFAULT_INTERVAL_DAYS)
        prev = last.get(sub)
        if not prev:
            due.add(sub); continue
        try:
            if (now - _dt.datetime.fromisoformat(prev)).days >= interval:
                due.add(sub)
        except Exception:
            due.add(sub)
    return due

def _mark_ran(subcats, now=None):
    import datetime as _dt
    now = now or _dt.datetime.now()
    last = _load_last_run()
    for s in subcats:
        last[s] = now.isoformat(timespec="seconds")
    _save_last_run(last)


def _derive_pillars(p):
    """Derive the key competitive 'pillars' for every product, from whatever
    specs + name/description we captured, normalised the same way across all
    three retailers. Only sets a pillar when there's positive evidence — a
    missing pillar stays absent rather than being guessed as 'no'."""
    import unicodedata
    specs = p.get("specs") or {}
    # haystack = all spec text + name, accent-stripped uppercase for robust matching
    blob = " ".join([str(p.get("name") or "")] + [f"{k} {v}" for k, v in specs.items()])
    H = "".join(c for c in unicodedata.normalize("NFD", blob.upper())
                if unicodedata.category(c) != "Mn")
    def has(*words): return any(w in H for w in words)

    # Cooling system
    if has("TOTAL NO FROST", "FULL NO FROST", "ΠΛΗΡΩΣ NO FROST"):
        specs["pillar_cooling"] = "Total No Frost"
    elif has("NO FROST"):
        specs["pillar_cooling"] = "No Frost"
    elif has("LOW FROST", "LESS FROST"):
        specs["pillar_cooling"] = "Low Frost"
    elif has("STATIC", "ΣΤΑΤΙΚ"):
        specs["pillar_cooling"] = "Static"

    # Connectivity / smart
    if has("WIFI", "WI-FI", "WI FI"):
        specs["pillar_wifi"] = "Yes"
    if has(" AI ", "ARTIFICIAL INTELLIGENCE", "ΤΕΧΝΗΤΗ ΝΟΗΜΟΣΥΝΗ", "AI "):
        specs["pillar_ai"] = "Yes"
    if has("SMART", "ΕΞΥΠΝ", "HOMEWHIZ", "THINQ", "SMARTTHINGS", "HOME CONNECT"):
        specs["pillar_smart"] = "Yes"

    # Water / ice dispenser (mostly multidoor / side-by-side)
    water = has("ΔΙΑΝΟΜΕΑΣ ΝΕΡΟΥ", "WATER DISPENSER", "ΠΑΡΟΧΗ ΝΕΡΟΥ", "DISPENSER ΝΕΡΟΥ")
    ice   = has("ΠΑΓΟΜΗΧΑΝ", "ICE MAKER", "ICE DISPENSER", "ΠΑΓΑΚΙ", "ICE")
    if water and ice:
        specs["pillar_dispenser"] = "Water + Ice"
    elif water:
        specs["pillar_dispenser"] = "Water only"
    elif ice:
        specs["pillar_dispenser"] = "Ice only"
    # tap-connected (plumbed) vs internal tank/box
    if water or ice:
        if has("ΣΥΝΔΕΣΗ ΝΕΡΟΥ", "PLUMBED", "ΠΑΡΟΧΗ ΔΙΚΤΥΟΥ", "ΣΥΝΔΕΣΗ ΣΤΟ ΔΙΚΤΥΟ", "WATER LINE"):
            specs["pillar_water_supply"] = "Plumbed (tap)"
        elif has("ΔΟΧΕΙΟ", "TANK", "RESERVOIR", "ΡΕΖΕΡΒΟΥΑΡ", "ΕΣΩΤΕΡΙΚΟ ΔΟΧΕΙΟ"):
            specs["pillar_water_supply"] = "Internal tank"

    p["specs"] = specs
    return p

def _ensure_color(p):
    """Guarantee a colour value for every product. Retailers often omit colour
    from the spec table, but Greek product names almost always end with it
    (e.g. '… Λευκό', '… Inox', '… Μαύρο'). If the spec is missing, derive the
    colour family from the name so the colour filter has reliable coverage."""
    specs = p.get("specs") or {}
    existing = (specs.get("Χρώμα") or specs.get("Color") or "").strip()
    if existing:
        return p
    name = (p.get("name") or "")
    # Uppercasing Greek adds diacritics (ύ→Ϋ, ά→Ά); strip accents so cues match.
    import unicodedata
    up = "".join(c for c in unicodedata.normalize("NFD", name.upper())
                 if unicodedata.category(c) != "Mn")
    # Greek + English colour cues, most specific first
    CUES = [
        ("Inox / Silver", ["INOX", "ΑΝΟΞΕΙΔΩΤ", "SILVER", "ΑΣΗΜ", "STAINLESS", "BRUSHED"]),
        ("Graphite / Dark", ["GRAPHITE", "ΓΡΑΦΙΤ", "ANTHRACITE", "ΑΝΘΡΑΚ", "ΣΚΟΥΡΟ", "TITAN"]),
        ("White", ["ΛΕΥΚ", "WHITE"]),
        ("Black", ["ΜΑΥΡ", "BLACK"]),
        ("Grey", ["ΓΚΡΙ", "GREY", "GRAY"]),
        ("Beige / Cream", ["ΜΠΕΖ", "BEIGE", "ΚΡΕΜ", "CREAM", "IVORY"]),
        ("Red", ["ΚΟΚΚΙΝ", "RED"]),
        ("Blue", ["ΜΠΛΕ", "BLUE", "ΓΑΛΑΖ"]),
        ("Green", ["ΠΡΑΣΙΝ", "GREEN"]),
    ]
    for fam, words in CUES:
        if any(w in up for w in words):
            specs["Χρώμα"] = fam
            p["specs"] = specs
            return p
    return p

def _ldjson_price(v):
    """ld+json prices are usually plain numerics: 2160, 2160.00, "2160.00".
    They may occasionally carry US thousands separators (2,160.00). Parse
    without mistaking a thousands dot for a decimal."""
    if v is None: return None
    s = str(v).strip()
    # pure number forms
    try:
        # if it has a comma as thousands (US) and a dot decimal: 2,160.00
        if "," in s and "." in s and s.rfind(".") > s.rfind(","):
            return float(s.replace(",", ""))
        # european in a json string (rare): 2.160,00
        if "," in s and "." in s and s.rfind(",") > s.rfind("."):
            return float(s.replace(".", "").replace(",", "."))
        # only comma -> could be decimal (2160,00) 
        if "," in s and "." not in s:
            return float(s.replace(",", "."))
        return float(s)
    except Exception:
        return price_to_float(s)

def price_to_float(text):
    if not text: return None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s: return None
    if "," in s:
        # European: comma is decimal, dot is thousands (1.159,00 -> 1159.00)
        s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        # dot present, no comma: decimal point only if it looks like one
        # (exactly 1 or 2 digits after the last dot, e.g. 1349.0 / 89.90)
        last = s.split(".")[-1]
        if s.count(".") == 1 and len(last) in (1, 2):
            pass               # already a clean decimal like 1349.0
        else:
            s = s.replace(".", "")   # thousands separators like 1.159
    try:
        return float(s)
    except Exception:
        return None

def best_visible_price(text):
    """Pick the real product price from page text. Greek retail pages show a
    financing line ('έως €19,99/μήνα', installments) whose euro figure is NOT
    the price. Collect all euro amounts, drop those that are themselves a
    per-month / installment figure, and return the largest remaining."""
    if not text: return None
    import unicodedata
    def strip(s):
        return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                       if unicodedata.category(c) != "Mn")
    cands = []
    for m in re.finditer(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", text):
        amt = price_to_float(m.group(1))
        if amt is None: continue
        # per-month suffix attached to THIS amount (accent-insensitive)
        after = strip(text[m.end():m.end()+8])
        if any(w in after for w in ["μην", "/month", "month", "/mo"]):
            continue
        # installment lead-in right before the figure
        before = strip(text[max(0, m.start()-10):m.start()])
        if any(w in before for w in ["δοσ", "ατοκ", "x36", "x24", "x12"]):
            continue
        cands.append(amt)
    return max(cands) if cands else None

def detect_site(url):
    if "kotsovolos" in url: return "kotsovolos"
    if "plaisio" in url: return "plaisio"
    if "public.gr" in url: return "public"
    return "unknown"

# -- backend ----------------------------------------------------------------
def push_results(results, _tries=6):
    """Push a batch to the backend. Transient failures (502/503/504, timeouts,
    connection drops — typically Railway restarting after a redeploy) are
    retried with backoff instead of crashing the whole crawl. Only a genuine
    auth failure (401) is fatal."""
    if not results: return {"saved": 0, "new_products": 0}
    delay = 5
    for attempt in range(1, _tries+1):
        try:
            r = requests.post(f"{API_URL}/api/ingest/results",
                              headers={"X-Ingest-Token": INGEST_TOKEN, "Content-Type": "application/json"},
                              json={"results": results}, timeout=180)
            if r.status_code == 401:
                log("X 401 Unauthorized - INGEST_TOKEN does not match Railway."); sys.exit(1)
            if r.status_code in (502, 503, 504):
                raise requests.exceptions.HTTPError(f"{r.status_code} transient")
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.RequestException,) as e:
            if attempt == _tries:
                log(f"  ⚠ push failed after {_tries} attempts ({str(e)[:50]}) — skipping this batch, continuing crawl.")
                return {"saved": 0, "new_products": 0, "skipped_batch": True}
            log(f"  ⏳ backend not ready ({str(e)[:40]}); retry {attempt}/{_tries} in {delay}s…")
            time.sleep(delay)
            delay = min(delay*2, 60)   # 5,10,20,40,60,60…
    return {"saved": 0, "new_products": 0}

def get_tracked_products():
    r = requests.get(f"{API_URL}/api/products", timeout=30); r.raise_for_status(); return r.json()

# -- browser ----------------------------------------------------------------
def make_chrome(headless=False):
    o = Options()
    if headless: o.add_argument("--headless=new")
    o.add_argument("--window-size=1500,1000")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--log-level=3")
    o.add_experimental_option("excludeSwitches", ["enable-logging"])
    o.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    try:
        return webdriver.Chrome(options=o)
    except Exception:
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=o)

def accept_cookies(driver):
    for by, sel in [(By.ID, "onetrust-accept-btn-handler"),
                    (By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"),
                    (By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowallSelection")]:
        try:
            WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, sel))).click()
            time.sleep(1); return
        except Exception:
            continue

# -- crawlers (collect product URLs -> {url: subcategory}) ------------------
def crawl_kotsovolos(driver, cats):
    found = {}
    for cat, subcat in cats:
        log(f"  [K crawl] {subcat}: {cat}")
        driver.get(cat); time.sleep(3); accept_cookies(driver)
        same, last = 0, 0
        for _ in range(200):
            for a in driver.find_elements(By.XPATH, '//a[@id="insider-product-link"]'):
                href = a.get_attribute("href")
                if href: found.setdefault(href.split("?")[0], subcat)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            clicked = False
            for xp in ['//p[contains(text(),"Φόρτωση επόμενων")]',
                       '//button[contains(.,"Φόρτωση επόμενων")]',
                       '//*[contains(text(),"Φόρτωση επόμενων")]']:
                try:
                    btn = driver.find_element(By.XPATH, xp)
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True; time.sleep(2.5); break
                except (NoSuchElementException, ElementClickInterceptedException, StaleElementReferenceException):
                    continue
            if len(found) == last:
                same += 1
            else:
                same, last = 0, len(found)
            if same >= 3 and not clicked:
                break
        log(f"      -> {len(found)} total so far")
    return found

def crawl_plaisio(driver, cats):
    """Page through ?page=N over HTTP until a page yields no new products."""
    import httpx
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
               "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
    link_re = re.compile(r'href="(/product/[^"#?]+_\d+)"')
    found = {}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for cat, subcat in cats:
            log(f"  [P crawl] {subcat}: {cat}")
            cat_total = 0
            page1_links = None
            for page in range(1, 200):
                url = cat if page == 1 else f"{cat}?page={page}"
                try:
                    html = client.get(url).text
                except Exception as e:
                    log(f"      ! page {page} error: {e}")
                    break
                links = {urljoin("https://www.plaisio.gr", m) for m in link_re.findall(html)}
                if page == 1:
                    page1_links = links
                new = links - set(found)
                if not new:
                    break
                for u in new: found.setdefault(u, subcat)
                cat_total += len(new)
            if not page1_links:
                log(f"      (HTTP empty, using browser for {cat})")
                cat_total += _crawl_plaisio_browser(driver, cat, found, subcat)
            log(f"      -> {cat_total} from this category")
    return found


def _crawl_plaisio_browser(driver, cat, found, subcat):
    """Fallback: click 'Επόμενο' in a real browser for one category."""
    added = 0
    driver.get(cat); time.sleep(2); accept_cookies(driver)
    same, last = 0, len(found)
    while True:
        for a in driver.find_elements(By.XPATH, '//a[contains(@href,"/product/") and contains(@href,"_")]'):
            href = a.get_attribute("href")
            if href:
                u = href.split("?")[0]
                if u not in found:
                    found[u]=subcat; added += 1
        try:
            nxt = WebDriverWait(driver, 6).until(EC.element_to_be_clickable(
                (By.XPATH, '//span[text()="Επόμενο"] | //a[contains(@aria-label,"επόμεν")]')))
            driver.execute_script("arguments[0].scrollIntoView(true);arguments[0].click();", nxt)
            time.sleep(2)
            if len(found) == last: same += 1
            else: same, last = 0, len(found)
            if same >= 2: break
        except (TimeoutException, ElementClickInterceptedException, NoSuchElementException, StaleElementReferenceException):
            break
    return added

def crawl_public(driver, cats):
    found = {}
    for cat, subcat in cats:
        log(f"  [Pub crawl] {subcat}: {cat}")
        url = cat if "?" in cat else cat + "?r=90"
        driver.get(url); time.sleep(3); accept_cookies(driver)
        same, last = 0, 0
        for _ in range(200):
            for a in driver.find_elements(By.CSS_SELECTOR, "a.product__gallery__image"):
                href = a.get_attribute("href")
                if href: found.setdefault(urljoin("https://www.public.gr", href.split("?")[0]), subcat)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            clicked = False
            for xp in ['//a[span[contains(text(),"Δες περισσότερα")]]',
                       '//button[contains(.,"Δες περισσότερα")]',
                       '//*[contains(text(),"Δες περισσότερα")]']:
                try:
                    btn = driver.find_element(By.XPATH, xp)
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True; time.sleep(2.5); break
                except (NoSuchElementException, ElementClickInterceptedException, StaleElementReferenceException):
                    continue
            if len(found) == last:
                same += 1
            else:
                same, last = 0, len(found)
            if same >= 3 and not clicked:
                break
        log(f"      -> {len(found)} total so far")
    return found

# -- scrapers (read one product page) ---------------------------------------
def scrape_kotsovolos(driver, url, prices_only=False):
    p = {"url": url, "site": "kotsovolos", "success": False, "specs": {}}
    driver.get(url)
    # Wait for the actual price element rather than a blind fixed sleep: fast
    # pages continue immediately, slow ones still get up to 12s. Falls back to
    # body presence so we never hang on an oddly-structured page.
    try:
        WebDriverWait(driver,12).until(EC.presence_of_element_located((By.ID,"insider-final-price-with-vat")))
    except Exception:
        try: WebDriverWait(driver,8).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        except Exception: pass
    accept_cookies(driver)
    def by_id(i):
        try: return driver.find_element(By.ID, i).text.strip()
        except Exception: return None
    try: p["name"] = driver.find_element(By.ID, "insider-title").text.strip()
    except Exception:
        try: p["name"] = driver.find_element(By.TAG_NAME,"h1").text.strip()
        except Exception: p["name"] = "N/A"
    p["price"] = price_to_float(by_id("insider-final-price-with-vat"))
    p["old_price"] = price_to_float(by_id("insider-cross-price-with-vat"))
    p["availability"] = by_id("insider-availability") or "N/A"
    # image
    try:
        p["image"] = driver.find_element(By.ID, "insider-image-link").get_attribute("src")
    except Exception:
        try:
            p["image"] = driver.find_element(By.XPATH, '//meta[@property="og:image"]').get_attribute("content")
        except Exception:
            p["image"] = None

    # In prices-only mode we stop here — skip the slow spec extraction below.
    if prices_only:
        p["success"] = p["price"] is not None
        return p

    # full mode needs the spec accordion — give it a short moment to render
    try:
        WebDriverWait(driver,6).until(EC.presence_of_element_located((By.ID,"characteristics-accordion")))
    except Exception:
        pass
    # Read the ENTIRE characteristics table in ONE browser round-trip and parse
    # it in Python. The previous version fired ~20 separate Selenium XPath
    # queries (one per wanted label), each with full browser latency — the main
    # reason Kotsovolos was slow. This collects every label→value pair at once,
    # losing no data while cutting the per-product spec time dramatically.
    try:
        pairs = driver.execute_script("""
            const out = {};
            // 1) the structured characteristics accordion (label/value row pairs)
            const sec = document.getElementById('characteristics-accordion');
            if (sec) {
                const rows = sec.querySelectorAll('div[class*="product-charactristics-row"]');
                for (let i=0; i+1 < rows.length; i+=2) {
                    const k=(rows[i].innerText||'').trim();
                    const v=(rows[i+1].innerText||'').trim();
                    if (k) out[k]=v;
                }
            }
            // 2) any label:value spans anywhere (fallback for energy/colour/etc.
            //    that sit outside the accordion). label spans end with ':'.
            document.querySelectorAll('span').forEach(s=>{
                const t=(s.innerText||'').trim();
                if (t.endsWith(':') && t.length<60) {
                    const key=t.slice(0,-1).trim();
                    if (out[key]) return;
                    // value = next sibling span / element with text
                    let node=s.parentElement, val='';
                    for (let hop=0; hop<3 && node && !val; hop++){
                        let sib=node.nextElementSibling;
                        while (sib){ const x=(sib.innerText||'').trim();
                            if (x && x!==t){ val=x; break; } sib=sib.nextElementSibling; }
                        node=node.parentElement;
                    }
                    if (val) out[key]=val.split("\\n")[0];
                }
            });
            return out;
        """) or {}
        for k, v in pairs.items():
            if k and v and not p["specs"].get(k):
                p["specs"][k] = v
    except Exception:
        pass

    p["success"] = p["price"] is not None
    return p

_FIN_KEY = re.compile(r"install|month|loan|credit|financ|deposit|δοσ|μηνια|μηνι|makesoffer|addon|add_on|service|warrant|εγγυ|recycl|ανακυκλ|protect|safetynet", re.I)
_PRICE_KEY = ("finalPrice", "sellingPrice", "salePrice", "currentPrice", "priceWithVat", "price")

def _walk_for_price(node, _in_fin=False):
    """Recursively search Next.js JSON for the product price. Public's JSON also
    contains a financing/installment object with a small `price` (the monthly
    payment, e.g. 19.99) — naively taking the first price key grabbed THAT,
    making every product €19,99. We now (a) skip any subtree under a financing-
    related key, and (b) prefer the actual selling-price key by priority
    (finalPrice/sellingPrice before a generic/crossed-out price)."""
    by_key = {}   # key -> first plausible value seen outside financing subtrees
    def add(k, v):
        if k not in by_key and isinstance(v, (int, float)) and 10 < v < 50000:
            by_key[k] = float(v)
    def walk(n, in_fin):
        if isinstance(n, dict):
            for k, v in n.items():
                fin = in_fin or bool(_FIN_KEY.search(str(k)))
                if not fin and k in _PRICE_KEY:
                    if isinstance(v, (int, float)): add(k, v)
                    elif isinstance(v, dict):
                        for vk in ("value", "amount", "gross"):
                            add(k, v.get(vk))
                walk(v, fin)
        elif isinstance(n, list):
            for v in n: walk(v, in_fin)
    walk(node, _in_fin)
    # return the highest-priority selling-price key that was found
    for k in _PRICE_KEY:
        if k in by_key: return by_key[k]
    return None

def scrape_plaisio(driver, url, prices_only=False):
    """Plaisio scrapes over HTTP/JSON (proven reliable) — no browser needed."""
    import httpx
    from bs4 import BeautifulSoup
    p = {"url": url, "site": "plaisio", "success": False, "specs": {}}
    m = re.search(r"_(\d+)$", urlparse(url).path)
    p["product_code"] = m.group(1) if m else None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
               "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as c:
            html = c.get(url).text
    except Exception as e:
        p["error"] = str(e); return p
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if h1 and h1.text.strip():
        p["name"] = h1.text.strip()
    else:
        og = soup.find("meta", {"property": "og:title"})
        p["name"] = (og.get("content", "").split("|")[0].strip() if og else "N/A") or "N/A"

    # image
    ogimg = soup.find("meta", {"property": "og:image"})
    p["image"] = ogimg.get("content") if ogimg else None

    # price: ld+json -> __NEXT_DATA__ -> visible € text
    # The schema.org Product carries the real price in offers.price (and
    # offers.priceSpecification.price). Add-on SERVICES (warranty, recycling)
    # live under makesOffer with their own small price — we must NOT read those.
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("@type") == "Product":
                    offers = item.get("offers") or {}        # NOT makesOffer
                    if isinstance(offers, list): offers = offers[0] if offers else {}
                    price = offers.get("price")
                    if price is None:
                        ps = offers.get("priceSpecification") or {}
                        if isinstance(ps, list): ps = ps[0] if ps else {}
                        price = ps.get("price")
                    if price is not None: p["price"] = price_to_float(str(price))
                    if offers.get("availability"): p["availability"] = offers["availability"].split("/")[-1]
        except Exception: continue
    if not p.get("price"):
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            try: p["price"] = _walk_for_price(json.loads(nd.string))
            except Exception: pass
    if not p.get("price"):
        p["price"] = best_visible_price(soup.get_text(" ", strip=True))

    if not p.get("availability"):
        lines = [el.get_text(strip=True) for el in soup.select("div.pdp-stock__line")]
        p["availability"] = " | ".join(l for l in lines if l) or "N/A"

    if not prices_only:
        for dl in soup.select("dl.pdp-full-characteristics__specifications, dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                lbl, val = dt.get_text(strip=True), dd.get_text(strip=True)
                if lbl and val and len(lbl) < 80: p["specs"][lbl] = val

    p["success"] = p.get("price") is not None
    return p

def scrape_public(driver, url, prices_only=False):
    """Public is React/JSON-rendered. Try httpx+JSON first (fast), then fall
    back to the browser if needed. Price comes from ld+json / __NEXT_DATA__."""
    import httpx
    from bs4 import BeautifulSoup
    p = {"url": url, "site": "public", "success": False, "specs": {}}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
               "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
    html = None
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as c:
            html = c.get(url).text
    except Exception:
        html = None

    def parse(html):
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1 and h1.text.strip(): p["name"] = h1.text.strip()
        else:
            og = soup.find("meta", {"property": "og:title"})
            p["name"] = (og.get("content","").split("|")[0].strip() if og else "N/A") or "N/A"
        # image
        if not p.get("image"):
            ogimg = soup.find("meta", {"property": "og:image"})
            p["image"] = ogimg.get("content") if ogimg else None
        # ld+json Product/Offer
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
                for item in (data if isinstance(data, list) else [data]):
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        offers = item.get("offers") or {}
                        if isinstance(offers, list): offers = offers[0] if offers else {}
                        if offers.get("price"):
                            # ld+json prices are usually plain US-format (2160.00) — parse directly
                            p["price"] = _ldjson_price(offers["price"])
                        if offers.get("availability"): p["availability"] = offers["availability"].split("/")[-1]
            except Exception: continue
        # __NEXT_DATA__ deep search
        if not p.get("price"):
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd and nd.string:
                try: p["price"] = _walk_for_price(json.loads(nd.string))
                except Exception: pass
        # visible €-text price (also used to SANITY-CHECK the ld+json price).
        # Uses the financing-aware extractor so monthly-installment figures
        # ("έως €19,99/μήνα") are never mistaken for the product price.
        visible = best_visible_price(soup.get_text(" ", strip=True))
        if not p.get("price") and visible: p["price"] = visible
        # sanity: only override the JSON price with the visible one when the visible
        # figure is itself a plausible product price (≥ €40) AND they disagree by >5x.
        # This catches JSON parse errors (2.16 vs 2160) without ever trusting a tiny
        # financing leftover. Ratio-based, so genuinely cheap items are safe.
        if p.get("price") and visible and visible >= 40:
            hi, lo = max(p["price"], visible), min(p["price"], visible)
            if hi/lo > 5: p["price"] = visible
        # specs: <strong>label:</strong> value
        if not prices_only:
            for strong in soup.find_all("strong"):
                t = strong.get_text(strip=True)
                if t.endswith(":") and len(t) < 80:
                    sib = strong.find_parent().find_next_sibling()
                    if sib:
                        val = sib.get_text(strip=True)
                        if val: p["specs"][t.rstrip(":").strip()] = val

    if html:
        try: parse(html)
        except Exception: pass

    # browser fallback if HTTP gave no price
    if not p.get("price"):
        try:
            driver.get(url); WebDriverWait(driver,15).until(EC.presence_of_element_located((By.TAG_NAME,"body"))); time.sleep(1.5)
            accept_cookies(driver)
            parse(driver.page_source)
        except Exception as e:
            p["error"] = str(e)

    p["success"] = p.get("price") is not None
    return p

SCRAPER = {"kotsovolos": scrape_kotsovolos, "plaisio": scrape_plaisio, "public": scrape_public}
CRAWLER = {"kotsovolos": crawl_kotsovolos, "plaisio": crawl_plaisio, "public": crawl_public}

# -- orchestration ----------------------------------------------------------
def _sku_id_from_url(url):
    """Extract a trailing numeric id from a product URL, if present."""
    m = re.search(r"(\d{4,})(?:[^\d]*)$", urlparse(url).path)
    return int(m.group(1)) if m else None

def scrape_and_push(driver, url_map, prices_only=False, known_urls=None, max_known_id=None):
    """url_map: dict {url: subcategory}. Attaches category to each result.
    In prices_only mode, known products are scraped for price+availability only;
    products that are NEW (URL not seen before, or id higher than max stored)
    always get a FULL spec scrape and are flagged is_new=True."""
    grand = {"saved":0, "new_products":0}; buf = []
    known_urls = known_urls or set()
    max_known_id = max_known_id or {}
    urls = list(url_map.keys())
    _t0 = time.time()
    for i, url in enumerate(urls, 1):
        # Heartbeat every 50 products: progress, rate, and ETA so a glance at the
        # log tells you it's alive and how long is left. (Note: if the Windows
        # console freezes from QuickEdit/a stray click, the whole process pauses
        # and even this stops — disable QuickEdit Mode to prevent that.)
        if i > 1 and i % 50 == 1:
            el = time.time() - _t0
            rate = (i-1) / el if el > 0 else 0
            remain = (len(urls) - (i-1)) / rate if rate > 0 else 0
            log(f"  ♥ heartbeat: {i-1}/{len(urls)} done · {rate:.2f}/s · ~{remain/60:.0f} min left")
        site = detect_site(url)
        fn = SCRAPER.get(site)
        if not fn:
            continue
        subcat = url_map.get(url) or "general"
        # Decide if this product is new (full scrape) or known (price-only).
        sid = _sku_id_from_url(url)
        is_new = (url not in known_urls) or (sid is not None and sid > max_known_id.get(site, 0))
        use_prices_only = prices_only and not is_new
        try:
            res = fn(driver, url, prices_only=use_prices_only)
            res["category"] = subcat
            if not use_prices_only:
                _ensure_color(res)     # colour coverage from name when spec missing
                _derive_pillars(res)   # cooling / wifi / ai / dispenser pillars
            if is_new: res["is_new"] = True
            tag = "NEW" if is_new else ("PX " if use_prices_only else "OK ")
            log(f"  [{i}/{len(urls)}] {tag if res['success'] else 'XX '} {site}/{subcat} EUR {res.get('price')}  {str(res.get('name'))[:32]}")
            buf.append(res)
        except Exception as e:
            log(f"  [{i}/{len(urls)}] XX {site} {str(e)[:60]}")
            buf.append({"url": url, "site": site, "category": subcat, "success": False, "error": str(e)})
        if len(buf) >= PUSH_EVERY:
            resp = push_results(buf); grand["saved"]+=resp.get("saved",0); grand["new_products"]+=resp.get("new_products",0)
            log(f"    pushed batch -> {resp}"); buf = []
        time.sleep(DELAY if not use_prices_only else max(0.3, DELAY*0.4))
    if buf:
        resp = push_results(buf); grand["saved"]+=resp.get("saved",0); grand["new_products"]+=resp.get("new_products",0)
        log(f"    pushed final -> {resp}")
    return grand

def _save_urls(site, url_map):
    """Save url<TAB>subcategory per line so re-scrape keeps subcategory tags."""
    try:
        with open(f"urls_{site}.txt", "w", encoding="utf-8") as f:
            for u, sc in url_map.items():
                f.write(f"{u}\t{sc}\n")
        return True
    except Exception as e:
        log(f"  (could not save urls_{site}.txt: {e})"); return False

def _load_urls(site):
    """Load url<TAB>subcategory map; tolerate old files that have URL only."""
    fn = f"urls_{site}.txt"; out = {}
    if not os.path.exists(fn): return out
    with open(fn, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln: continue
            if "\t" in ln:
                u, sc = ln.split("\t", 1); out[u] = sc
            else:
                out[ln] = "general"
    return out

def _plp_row_to_result(row, canonical_subcat):
    """Map a standalone-fetcher row into the backend result schema. Flat spec
    fields become a specs{} dict (Greek labels). Laundry rows carry kg/rpm/
    programs/etc. and are tagged department=laundry so the dashboard keeps them
    in their own section, never blended with cooling."""
    if row.get("department") == "laundry":
        specs = {"__dept": "laundry"}
        if row.get("wash_kg"):    specs["Χωρητικότητα Πλύσης (kg)"] = row["wash_kg"]
        if row.get("dry_kg"):     specs["Χωρητικότητα Στεγνώματος (kg)"] = row["dry_kg"]
        if row.get("rpm"):        specs["Στροφές (rpm)"] = row["rpm"]
        if row.get("energy"):     specs["Ενεργειακή κλάση"] = row["energy"]
        if row.get("dry_energy"): specs["Ενεργειακή κλάση στεγνώματος"] = row["dry_energy"]
        if row.get("programs"):   specs["Προγράμματα"] = row["programs"]
        if row.get("steam"):      specs["Πρόγραμμα ατμού"] = row["steam"]
        if row.get("heat_pump"):  specs["Αντλία θερμότητας"] = row["heat_pump"]
        if row.get("condenser"):  specs["Σύστημα συμπύκνωσης"] = row["condenser"]
        if row.get("dimensions"): specs["Διαστάσεις"] = row["dimensions"]
        if row.get("loader"):
            specs["Τύπος φόρτωσης"] = "Top-load" if row["loader"] == "top" else "Front-load"
            specs["__loader"] = row["loader"]
        res = {
            "url": row.get("url", ""),
            "site": row.get("site", ""),
            "name": row.get("name", ""),
            "price": row.get("price"),
            "success": row.get("price") is not None,
            "category": canonical_subcat,        # washing_machine / washer_dryer / dryer
            "specs": specs,
        }
        if row.get("image"):      res["image"] = row["image"]
        if row.get("list_price"): res["old_price"] = row["list_price"]
        _sid = row.get("sku") or row.get("sku_id")
        if _sid: res["sku"] = str(_sid)     # retailer's own SKU id — stable identity for matching
        return res

    specs = {}
    if row.get("energy"):     specs["Ενεργειακή κλάση"] = row["energy"]
    if row.get("capacity"):   specs["Χωρητικότητα"] = row["capacity"]
    if row.get("cooling"):    specs["Τύπος Ψύξης"] = row["cooling"]
    if row.get("dimensions"): specs["Διαστάσεις"] = row["dimensions"]
    if row.get("weight"):     specs["Βάρος"] = row["weight"]
    if row.get("warranty"):   specs["Εγγύηση"] = row["warranty"]
    if row.get("fridge_lt"):  specs["Καθαρή χωρητικότητα συντήρησης"] = row["fridge_lt"]
    if row.get("freezer_lt"): specs["Καθαρή χωρητικότητα κατάψυξης"] = row["freezer_lt"]
    res = {
        "url": row.get("url", ""),
        "site": row.get("site", ""),
        "name": row.get("name", ""),
        "price": row.get("price"),
        "success": row.get("price") is not None,
        "category": canonical_subcat,
        "specs": specs,
    }
    if row.get("availability"): res["availability"] = row["availability"]
    if row.get("list_price"):   res["old_price"] = row["list_price"]
    if row.get("image"):        res["image"] = row["image"]
    _sid = row.get("sku") or row.get("sku_id")
    if _sid: res["sku"] = str(_sid)         # retailer's own SKU id — stable identity for matching
    return res


def run_plp(sites, scope_subs, headless=True):
    """FAST catalogue path. For each site, pull the whole category list (price +
    basic specs) via the standalone API fetchers, map to result rows, enrich,
    and push in batches. Replaces both the crawl and the per-PDP price scrape
    for the daily run — new products appear in the listing and auto-register."""
    if not _PLP_OK:
        log(f"X --plp needs public_plp.py / kotsovolos_plp.py / plaisio_plp.py "
            f"in this folder (import failed: {_PLP_ERR}).")
        sys.exit(1)

    grand = {"saved": 0, "new_products": 0}
    buf = []

    def flush():
        if not buf:
            return
        for r in buf:
            _ensure_color(r)
            _derive_pillars(r)
        resp = push_results(buf)
        grand["saved"] += resp.get("saved", 0)
        grand["new_products"] += resp.get("new_products", 0)
        log(f"    pushed {len(buf)} -> {resp}")
        buf.clear()

    def in_scope(canon):
        return (scope_subs is None) or (canon in scope_subs)

    for site in sites:
        smap = PLP_SUBCAT_MAP[site]

        if site == "public":
            for key, path in public_plp.CATEGORIES.items():
                canon = smap.get(key, key)
                if not in_scope(canon):
                    continue
                log(f"[PLP] public/{key} -> {canon}")
                try:
                    rows, total = public_plp.collect_all(path, cat_name=key, debug=False)
                except Exception as e:
                    log(f"   ! public/{key} failed: {str(e)[:70]}"); continue
                for row in rows:
                    row["site"] = "public"
                    buf.append(_plp_row_to_result(row, canon))
                    if len(buf) >= PUSH_EVERY: flush()
                log(f"   public/{key}: {len(rows)} (of ~{total})")
            # ---- laundry department (separate LAUNDRY dict; own spec map) ----
            pub_loader = {}
            _lk = [k for k in getattr(public_plp, "LAUNDRY", {})
                   if scope_subs is None or k in scope_subs]
            if "washing_machine" in _lk:
                try:
                    pub_loader = public_plp.loader_map()
                except Exception as e:
                    log(f"   ! public loader map failed: {str(e)[:60]}"); pub_loader = {}
            for key, path in getattr(public_plp, "LAUNDRY", {}).items():
                if scope_subs is not None and key not in scope_subs:
                    continue
                log(f"[PLP] public/{key} -> laundry")
                try:
                    rows, total = public_plp.collect_all(
                        path, cat_name=key, debug=False,
                        spec_map=public_plp.LAUNDRY_SPEC_MAP, dept="laundry")
                except Exception as e:
                    log(f"   ! public/{key} failed: {str(e)[:70]}"); continue
                for row in rows:
                    row["site"] = "public"
                    row["department"] = "laundry"
                    if key == "washing_machine":
                        row["loader"] = pub_loader.get(str(row.get("sku_id", "")), "")
                    elif key == "washer_dryer":
                        row["loader"] = "front"
                    buf.append(_plp_row_to_result(row, key))
                    if len(buf) >= PUSH_EVERY: flush()
                log(f"   public/{key} [laundry]: {len(rows)} (of ~{total})")
            flush()

        elif site == "kotsovolos":
            session = requests.Session()
            session.headers.update(kotsovolos_plp.HEADERS)
            for key, catid in kotsovolos_plp.CATEGORIES.items():
                canon = smap.get(key, key)
                if not in_scope(canon):
                    continue
                log(f"[PLP] kotsovolos/{key} -> {canon}")
                try:
                    rows, total = kotsovolos_plp.collect_category(session, catid, key, debug=False)
                except Exception as e:
                    log(f"   ! kotsovolos/{key} failed: {str(e)[:70]}"); continue
                for row in rows:
                    buf.append(_plp_row_to_result(row, canon))
                    if len(buf) >= PUSH_EVERY: flush()
                log(f"   kotsovolos/{key}: {len(rows)} (of ~{total})")
            # ---- laundry (resolve catId from the SEO url, then same API) ----
            for key, lurl in getattr(kotsovolos_plp, "LAUNDRY", {}).items():
                if scope_subs is not None and key not in scope_subs:
                    continue
                cid = kotsovolos_plp.resolve_cat_id(session, lurl)
                if not cid:
                    log(f"   ! kotsovolos/{key} [laundry]: catId unresolved, skipping"); continue
                log(f"[PLP] kotsovolos/{key} -> laundry (catId {cid})")
                try:
                    rows, total = kotsovolos_plp.collect_category(session, cid, key, debug=False, dept="laundry")
                except Exception as e:
                    log(f"   ! kotsovolos/{key} failed: {str(e)[:70]}"); continue
                for row in rows:
                    row["department"] = "laundry"
                    buf.append(_plp_row_to_result(row, key))
                    if len(buf) >= PUSH_EVERY: flush()
                log(f"   kotsovolos/{key} [laundry]: {len(rows)} (of ~{total})")
            flush()

        elif site == "plaisio":
            pdriver = plaisio_plp.make_chrome(headless=headless)
            try:
                for key, path in plaisio_plp.CATEGORIES.items():
                    canon = smap.get(key, key)
                    if not in_scope(canon):
                        continue
                    log(f"[PLP] plaisio/{key} -> {canon}")
                    try:
                        rows, total = plaisio_plp.collect_category(pdriver, path, key, debug=False)
                    except Exception as e:
                        log(f"   ! plaisio/{key} failed: {str(e)[:70]}"); continue
                    for row in rows:
                        buf.append(_plp_row_to_result(row, canon))
                        if len(buf) >= PUSH_EVERY: flush()
                    log(f"   plaisio/{key}: {len(rows)} (of ~{total})")
                # ---- laundry (same hydrated-ItemList path, own LAUNDRY paths) ----
                pla_loader = {}
                _plk = [k for k in getattr(plaisio_plp, "LAUNDRY", {})
                        if scope_subs is None or k in scope_subs]
                if "washing_machine" in _plk:
                    try:
                        pla_loader = plaisio_plp.loader_map(pdriver)
                    except Exception as e:
                        log(f"   ! plaisio loader map failed: {str(e)[:60]}"); pla_loader = {}
                for key, path in getattr(plaisio_plp, "LAUNDRY", {}).items():
                    if scope_subs is not None and key not in scope_subs:
                        continue
                    log(f"[PLP] plaisio/{key} -> laundry")
                    try:
                        rows, total = plaisio_plp.collect_category(pdriver, path, key, debug=False, dept="laundry")
                    except Exception as e:
                        log(f"   ! plaisio/{key} failed: {str(e)[:70]}"); continue
                    for row in rows:
                        row["department"] = "laundry"
                        if key == "washing_machine":
                            row["loader"] = pla_loader.get(str(row.get("sku_id", "")), "")
                        elif key == "washer_dryer":
                            row["loader"] = "front"
                        buf.append(_plp_row_to_result(row, key))
                        if len(buf) >= PUSH_EVERY: flush()
                    log(f"   plaisio/{key} [laundry]: {len(rows)} (of ~{total})")
            finally:
                pdriver.quit()
            flush()

    flush()
    return grand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", choices=["kotsovolos","plaisio","public"], help="only this site")
    ap.add_argument("--quick", action="store_true", help="skip crawl; re-scrape known products from backend")
    ap.add_argument("--rescrape", action="store_true", help="skip crawl; re-scrape URLs saved from the last crawl (urls_<site>.txt)")
    ap.add_argument("--prices-only", "--fast", dest="prices_only", action="store_true",
                    help="FAST recurring run: quick crawl to find new products, price+availability for known ones, full specs only for new")
    ap.add_argument("--headless", action="store_true", help="hide browser window")
    ap.add_argument("--due", action="store_true",
                    help="only scrape subcategories whose interval has elapsed (niche segments scraped less often)")
    ap.add_argument("--only", help="comma-separated subcategories to scrape (e.g. wine_cooler,mini_bar)")
    ap.add_argument("--plp", action="store_true",
                    help="FAST catalogue mode: pull each retailer's whole list (price + basic specs) "
                         "via the new APIs. Recommended daily price path; replaces crawl+PDP for prices.")
    ap.add_argument("--max-minutes", type=int, default=0, dest="max_minutes",
                    help="fail-safe for scheduled runs: force-exit after N minutes if the run hangs "
                         "(e.g. --max-minutes 40). 0 = off.")
    args = ap.parse_args()
    _start_watchdog(args.max_minutes)
    if not INGEST_TOKEN or INGEST_TOKEN.startswith("PASTE_"):
        log("X Set the INGEST_TOKEN environment variable first (must match Railway)."); sys.exit(1)

    # Decide which subcategories are in scope for this run.
    if args.only:
        scope_subs = {s.strip() for s in args.only.split(",") if s.strip()}
        log(f"Scope: --only {sorted(scope_subs)}")
    elif args.due:
        scope_subs = _due_subcats()
        skipped = _all_subcats() - scope_subs
        log(f"Scope: --due -> scraping {sorted(scope_subs)}")
        if skipped: log(f"        skipping (not due yet): {sorted(skipped)}")
        if not scope_subs:
            log("Nothing due today — exiting."); return
    else:
        scope_subs = None  # all

    def _cats_for(site):
        """CATEGORIES for a site, filtered to the in-scope subcategories."""
        cats = CATEGORIES[site]
        if scope_subs is None:
            return cats
        return [(u, sub) for (u, sub) in cats if sub in scope_subs]

    # ---- FAST catalogue mode (new APIs): no crawl, no PDP, no main browser ----
    if args.plp:
        sites = [args.site] if args.site else ["public", "kotsovolos", "plaisio"]
        log(f"=== PLP catalogue mode (fast price path) — sites: {sites} ===")
        grand = run_plp(sites, scope_subs, headless=True)
        log(f"DONE (PLP) -> {grand}")
        # Retire delisted/zombie products — only after a FULL sweep (all sites,
        # all categories), so partial runs (--site / --only) never wrongly retire
        # products from sites/categories they didn't touch this run.
        full_sweep = (args.site is None) and (scope_subs is None)
        if full_sweep:
            try:
                rr = requests.post(f"{API_URL}/api/ingest/retire",
                                   headers={"X-Ingest-Token": INGEST_TOKEN},
                                   timeout=120)
                log(f"Retire stale/zombie -> {rr.json() if rr.ok else rr.status_code}")
            except Exception as e:
                log(f"  (retire call failed, non-fatal: {str(e)[:60]})")
        if args.due or scope_subs is not None:
            ran = scope_subs if scope_subs is not None else _all_subcats()
            _mark_ran(ran)
            log(f"Marked as scraped: {sorted(ran)}")
        return

    driver = make_chrome(headless=args.headless)
    try:
        sites = [args.site] if args.site else ["plaisio","kotsovolos","public"]

        # ---- FAST recurring mode: quick crawl + price-only known + full for new ----
        if args.prices_only:
            # what do we already know? (URLs from cache + highest id per site)
            known = {}; max_id = {}
            for s in sites:
                m = _load_urls(s)
                known.update(m)
                ids = [i for i in (_sku_id_from_url(u) for u in m) if i]
                max_id[s] = max(ids) if ids else 0
            known_urls = set(known.keys())
            # quick crawl just to DISCOVER current URLs (this is the light part)
            url_map = {}
            for s in sites:
                log(f"=== Quick crawl {s} (discover URLs) ===")
                m = CRAWLER[s](driver, _cats_for(s))
                _save_urls(s, m)             # refresh cache so new items persist
                url_map.update(m)
            new_count = len([u for u in url_map if u not in known_urls])
            log(f"Fast run: {len(url_map)} URLs ({new_count} new). Price-only for known, full specs for new...")
            log(f"DONE -> {scrape_and_push(driver, url_map, prices_only=True, known_urls=known_urls, max_known_id=max_id)}")
            return

        if args.quick:
            prods = get_tracked_products()
            url_map = {p["url"]: (p.get("category") or "general") for p in prods}
            log(f"Quick mode: re-scraping {len(url_map)} known products.")
            log(f"DONE -> {scrape_and_push(driver, url_map)}"); return

        if args.rescrape:
            url_map = {}
            for s in sites:
                m = _load_urls(s)
                log(f"  loaded {len(m)} saved URLs for {s}")
                url_map.update(m)
            log(f"Re-scrape mode: {len(url_map)} URLs. Scraping now...")
            log(f"DONE -> {scrape_and_push(driver, url_map)}"); return

        all_map = {}
        for s in sites:
            log(f"=== Crawling {s} ===")
            m = CRAWLER[s](driver, _cats_for(s))   # {url: subcat}
            _save_urls(s, m)
            log(f"  -> {s}: {len(m)} product URLs (saved with subcategory tags)")
            all_map.update(m)
        log(f"TOTAL discovered: {len(all_map)}. Scraping now...")
        log(f"DONE -> {scrape_and_push(driver, all_map)}")
        ran = scope_subs if scope_subs is not None else _all_subcats()
        _mark_ran(ran)
        log(f"Marked as scraped: {sorted(ran)}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()