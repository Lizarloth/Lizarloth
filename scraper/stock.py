#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock.py  -  Pricedge: one stock status per product, across retailers.

Canonical statuses (stored as the availability string):
  InStock       Άμεσα διαθέσιμο
  LastPieces    Τελευταία τεμάχια
  OnOrder       Κατόπιν παραγγελίας / Διαθέσιμο με παραγγελία / pre-order
  StoreOnly     Αγορά (Διαθέσιμο) μόνο από κατάστημα — not sold online
  Display       Εκθεσιακό προϊόν — display unit
  Partner       sold by a marketplace partner (Public Partner)
  OutOfStock    Εξαντλήθηκε / Μη διαθέσιμο
  ""            not reported (the listing APIs don't say; see --check-stock)

Where each retailer's truth lives:
  Public      /public/v2/sku/{id} -> sku.availability (18 KB JSON per product)
  Kotsovolos  the product page's stock chip (≈1 MB HTML per product)
  Plaisio     the listing card itself (free; set by plaisio_plp)
"""

import re
import time

import requests

from cooking import fold

_RULES = [  # order matters: most specific first
    ("Display",    r"ΕΚΘΕΣΙΑΚ"),
    ("LastPieces", r"ΕΛΕΥΤΑΙΑ ΤΕΜΑΧ"),   # Kotsovolos spells it with a Latin "T"
    ("StoreOnly",  r"ΜΟΝΟ ΑΠΟ ΚΑΤΑΣΤΗΜΑ|INSTOREONLY|STOREONLY"),
    ("Partner",    r"PUBLIC PARTNER|ΣΥΝΕΡΓΑΤ"),
    ("OnOrder",    r"ΠΑΡΑΓΓΕΛΙ|PREORDER|PRE-ORDER|BACKORDER|ONORDER"),
    ("OutOfStock", r"ΕΞΑΝΤΛΗΘ|ΜΗ ΔΙΑΘΕΣΙΜ|OUTOFSTOCK|SOLDOUT|DISCONTINUED"),
    ("InStock",    r"ΑΜΕΣΑ ΔΙΑΘΕΣΙΜ|^ΔΙΑΘΕΣΙΜΟ$|INSTOCK|IN STOCK|LIMITEDAVAILABILITY"),
]
_COMPILED = [(k, re.compile(rx)) for k, rx in _RULES]


def normalize(text):
    """Retailer wording / schema.org value -> canonical status ('' if unknown)."""
    t = fold(re.sub(r"https?://schema\.org/", "", str(text or ""))).strip()
    if not t:
        return ""
    for key, rx in _COMPILED:
        if rx.search(t):
            return key
    return ""


# ---- Public: the per-product JSON the product page itself loads ----
PUBLIC_SKU_API = "https://www.public.gr/public/v2/sku/{}"


def public_status(session, sku_id):
    r = session.get(PUBLIC_SKU_API.format(sku_id), params={"locale": "el"}, timeout=30)
    r.raise_for_status()
    sku = r.json().get("sku") or {}
    return normalize(sku.get("availability")), sku.get("availability") or ""


# ---- Kotsovolos: the stock chip on the product page ----
_K_CHIP = re.compile(r'insider-availability"><span class="MuiChip-label">([^<]+)<')
_K_SCHEMA = re.compile(r'availability":"https://schema.org/(\w+)"')


def kotsovolos_status(session, url):
    """Chip first ("Άμεσα διαθέσιμο", "Τελευταία τεμάχια", "Εκθεσιακό προϊόν"),
    then the store-only notice, then the page's schema.org availability."""
    r = session.get(url, timeout=40, headers={"Accept": "text/html"})
    if r.status_code in (403, 429):     # blocked / rate-limited: count as a failure
        r.raise_for_status()
    t = r.text
    m = _K_CHIP.search(t)
    if m:
        st = normalize(m.group(1))
        if st:
            return st, m.group(1).strip()
    if "μόνο από κατάστημα" in t:
        return "StoreOnly", "Διαθέσιμο μόνο από κατάστημα"
    m = _K_SCHEMA.search(t)
    return (normalize(m.group(1)), m.group(1)) if m else ("", "")


def check(targets, log=print, pause=0.35, headers=None):
    """targets: [(site, url, sku_id)] for public/kotsovolos. Returns
    [{'url', 'availability'}] for every product whose status could be read."""
    s = requests.Session()
    if headers:
        s.headers.update(headers)
    out, failed = [], 0
    t0 = time.time()
    # per site: if its first 10 requests all fail (e.g. a 403 bot block), stop
    # asking it this run - more requests only prolong a block
    tried, ok, given_up = {}, {}, set()
    for i, (site, url, sku) in enumerate(targets, 1):
        if site in given_up:
            continue
        tried[site] = tried.get(site, 0) + 1
        if tried[site] > 10 and not ok.get(site):
            given_up.add(site)
            log(f"   ! stock: {site} refused its first 10 requests - skipping it this run")
            continue
        try:
            if site == "public" and sku:
                st, raw = public_status(s, sku)
            elif site == "kotsovolos":
                st, raw = kotsovolos_status(s, url)
            else:
                continue
        except Exception as e:
            failed += 1
            if failed <= 5:
                log(f"   ! stock {site} {str(e)[:50]}")
            continue
        ok[site] = ok.get(site, 0) + 1
        if st:
            out.append({"url": url, "availability": st})
        if i % 100 == 0:
            log(f"   stock: {i}/{len(targets)} checked · {time.time()-t0:.0f}s")
        time.sleep(pause)
    return out
