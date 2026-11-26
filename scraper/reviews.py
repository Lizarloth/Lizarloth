#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reviews.py  -  Pricedge: customer reviews as Kotsovolos's product pages show
them, for the Brand perception page (what buyers say about design, price,
build, technology, noise...).

The scraper's own browser opens the product page the same way it does for
prices, accepts the cookie banner as it already does, and reads the review
list the page renders (Yotpo widget): stars, title, text, date, verified
buyer. No reviewer names are kept. Up to MAX_PAGES pages (5 reviews each,
newest first) per product.

A product is read again only when its review count (from the listing) has
grown since the last read - reviews_seen.json remembers the count. Your
models first, then the most-reviewed rivals, within a time budget per run.
Plaisio's ratings come from Feefo's public API in local_runner instead.
"""

import datetime as dt
import hashlib
import json
import os
import time

MAX_PAGES = 4            # 5 reviews a page -> the 20 newest per product
PAUSE = 1.5              # seconds between products
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews_seen.json")

# one review per .yotpo-review: stars from the rating's label ("5 από 5 αστέρια"),
# falling back to the number of full stars drawn
_JS_READ = r"""
return [...document.querySelectorAll('.yotpo-review')].map(r => {
  const q = s => r.querySelector(s);
  const st = q('.yotpo-star-rating');
  const lab = st ? (st.getAttribute('aria-label') || st.getAttribute('title') || '') : '';
  const m = lab.match(/([\d.,]+)/);
  const stars = m ? parseFloat(m[1].replace(',', '.')) : (st ? st.querySelectorAll('.yotpo-sr-star-full').length : null);
  const t = s => { const e = q(s); return e ? e.innerText.trim() : ''; };
  return {stars, title: t('.yotpo-review-title'), text: t('.yotpo-review-content'),
          date: t('.yotpo-date-format') || t('.yotpo-review-date'),
          verified: !!q('.yotpo-reviewer-verified-buyer-text')};
});"""

_JS_NEXT = r"""
const want = String(arguments[0]);
const el = [...document.querySelectorAll('.yotpo-reviews-pagination-item')].find(e => e.innerText.trim() === want);
if (!el) return false;
el.scrollIntoView({block: 'center'}); el.click(); return true;"""

_JS_FIRST = "const e=document.querySelector('.yotpo-review .yotpo-review-content'); return e ? e.innerText.slice(0,80) : '';"


def _load():
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(seen):
    with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, ensure_ascii=False, indent=0, sort_keys=True)


def _iso(d):
    """'08/11/26' or '08/11/2026' (day first, as the page shows) -> '2026-11-08'."""
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(d.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            continue
    return None


def _wait(driver, js, ok, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if ok(driver.execute_script(js)):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def read_product(driver, url):
    """The newest reviews on one product page, up to MAX_PAGES pages."""
    driver.get(url + ("&" if "?" in url else "?") + "showReviews=true")
    driver.execute_script("const s=document.querySelector('#reviews,.reviewsSec'); s&&s.scrollIntoView();")
    if not _wait(driver, "return document.querySelectorAll('.yotpo-review').length", lambda n: n > 0, 15):
        return []
    out = []
    for page in range(1, MAX_PAGES + 1):
        rows = driver.execute_script(_JS_READ) or []
        out += rows
        if page == MAX_PAGES or len(rows) < 5:
            break
        first = driver.execute_script(_JS_FIRST)
        if not driver.execute_script(_JS_NEXT, page + 1):
            break
        if not _wait(driver, _JS_FIRST, lambda t: t and t != first, 8):
            break
    return out


def run(targets, push, log=print, budget_minutes=20, make_driver=None, accept_cookies=None):
    """targets: [{site, url, name, reviews}] in priority order. push(items)
    sends a batch to /api/ingest/reviews."""
    t_end = time.time() + budget_minutes * 60
    seen = _load()
    todo = [t for t in targets if t.get("site") == "kotsovolos" and (t.get("reviews") or 0) > 0
            and seen.get(t["url"]) != t["reviews"]]
    log(f"[reviews] {len(todo)} Kotsovolos products with reviews not read yet")
    if not todo:
        return
    driver = make_driver()
    batch, done, got, fails = [], 0, 0, 0
    try:
        for i, t in enumerate(todo):
            if time.time() > t_end:
                log(f"   reviews: time budget reached after {done} products ({len(todo) - done} left for the next run)")
                break
            try:
                if i == 0:
                    driver.get(t["url"]); time.sleep(2); accept_cookies(driver)
                rows = read_product(driver, t["url"])
                fails = 0
            except Exception as e:
                fails += 1
                log(f"   ! reviews {t.get('name', '')[:40]}: {str(e)[:60]}")
                if fails >= 5:
                    log("   ! reviews: 5 errors in a row - stopping for today")
                    break
                continue
            for r in rows:
                text, title, date = (r.get("text") or "").strip(), (r.get("title") or "").strip(), _iso(r.get("date") or "")
                if not (text or title):
                    continue
                key = hashlib.sha1("|".join([t["url"], date or "", title, text[:300]]).encode("utf-8")).hexdigest()
                stars = r.get("stars")
                batch.append({"key": key, "site": "kotsovolos", "url": t["url"], "product_name": t.get("name"),
                              "stars": int(round(stars)) if isinstance(stars, (int, float)) and stars else None,
                              "title": title, "text": text, "date": date, "verified": bool(r.get("verified"))})
                got += 1
            seen[t["url"]] = t["reviews"]
            done += 1
            if len(batch) >= 100:
                push(batch); batch = []; _save(seen)
            if done % 25 == 0:
                log(f"   reviews: {done}/{len(todo)} products, {got} reviews read")
            time.sleep(PAUSE)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if batch:
            push(batch)
        _save(seen)
    log(f"[reviews] DONE: {done} products, {got} reviews read")
