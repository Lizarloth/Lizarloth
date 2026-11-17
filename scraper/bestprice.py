#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bestprice.py  -  Pricedge: the whole Greek market's offers per model, from
BestPrice's public read-only MCP service (https://mcp.bestprice.gr/mcp).

BestPrice OK'd Pricedge reading what it allows (Nov 2026). Its web pages
refuse scripts, but the MCP endpoint is its published machine interface,
so that's the only route used here - plain JSON-RPC over HTTPS, an honest
User-Agent, a gentle pace, and a stop on repeated errors.

Per model:
  search_products   -> BestPrice product id (cached in bestprice_ids.json)
  compare_offers    -> up to 10 cheapest offers, plus up to 10 best-rated
                       ones when the model has more than 10 shops (that is
                       where Public usually shows up)
The readings go to /api/ingest/market, which keeps lowest / median / shop
count per model and fills in Public's price while Public can't be scraped.
"""

import datetime as dt
import json
import os
import re
import time

import requests

MCP_URL = "https://mcp.bestprice.gr/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "User-Agent": "Pricedge/1.0 (price monitoring)"}
PAUSE = 1.0                 # seconds between calls
RETRY_MISSES_DAYS = 7       # re-search models BestPrice didn't have after a week
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bestprice_ids.json")


# ---- model code (port of the backend's extract_model; keep in sync) ----
_BRANDS = ["WHIRLPOOL", "SAMSUNG", "LG", "BOSCH", "SIEMENS", "PITSOS", "DELONGHI", "DE LONGHI", "AEG",
           "BEKO", "HISENSE", "TOSHIBA", "SHARP", "CANDY", "LIEBHERR", "MIELE", "INVENTOR", "MORRIS", "UNITED",
           "CROWN", "MIDEA", "HAIER", "TCL", "DAVOLINE", "ELECTROLUX", "INDESIT", "HOTPOINT", "GORENJE", "TEKA",
           "FINLUX", "PHILCO", "FRANKE", "NEFF", "SMEG", "BERTAZZONI", "KENDRIX", "OMNYS", "PROFI COOK", "PROFICOOK",
           "LA SOMMELIERE", "JURO PRO", "JURO-PRO", "ESKIMO", "ROBIN", "CARAD", "TELEFUNKEN", "NASCO", "KARAMCO",
           "ELICA", "FABER", "LA GERMANIA", "PYRAMIS", "CECOTEC", "KÜPPERSBUSCH", "KUPPERSBUSCH", "HOOVER", "BRANDT",
           "ZANUSSI", "SINGER", "ROHNSON", "IZZY", "ESTIA"]
_LOOKALIKE = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX")
_STOP = {"LT", "L", "CM", "ML", "KG", "DB", "W", "KWH", "NO", "FROST", "TOTAL", "LOW", "FULL", "EASY", "STATIC",
         "INOX", "WHITE", "BLACK", "SILVER", "INVERTER", "CLASS", "ENERGY", "GALA", "ED", "EU", "PEAK", "BIOFRESH",
         "GRANDCRU", "SELECTION", "VINIDOR", "COLLECTION", "FRESH", "PRO", "PLUS", "SMART"}
_UNITS = {"LT", "L", "CM", "ML", "KG", "DB", "W", "KWH"}
_GREEK = re.compile("[Ͱ-Ͽἀ-῿]")


def extract_model(name):
    """(brand, normalized code) from a product name."""
    if not name:
        return "", ""
    up = str(name).upper().translate(_LOOKALIKE)
    brand = next((b for b in _BRANDS if b in up), "")
    rest = up[up.index(brand) + len(brand):] if brand else up
    tokens = [t for t in re.sub(r"[^\w ]+", " ", rest).replace("_", " ").split() if t]
    code = []
    for i, t in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if _GREEK.search(t) or t in _STOP:
            break
        if re.fullmatch(r"\d{2,4}", t) and (nxt is None or nxt in _UNITS or (nxt and _GREEK.search(nxt))):
            break
        if (re.search(r"\d", t)
                or (len(t) <= 5 and t.isalpha() and nxt and re.search(r"\d", nxt))
                or (len(t) <= 4 and t.isalpha() and code)):
            code.append(t)
        elif code:
            break
        if len(code) >= 6:
            break
    return brand.replace(" ", ""), re.sub(r"[^A-Z0-9]", "", "".join(code))


# ---- MCP client ----
class Mcp:
    def __init__(self):
        self.s = requests.Session()
        self.h = dict(HEADERS)
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "pricedge", "version": "1.0"}})
        self._rpc("notifications/initialized", notify=True)

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            body["id"] = self.n
        r = self.s.post(MCP_URL, headers=self.h, json=body, timeout=45)
        if r.headers.get("mcp-session-id"):
            self.h["Mcp-Session-Id"] = r.headers["mcp-session-id"]
        r.raise_for_status()
        if notify:
            return None
        t = r.text
        if "data:" in t[:200]:
            t = "\n".join(l[5:] for l in t.splitlines() if l.startswith("data:"))
        res = json.loads(t)
        if res.get("error"):
            raise RuntimeError(str(res["error"])[:120])
        return res.get("result") or {}

    def call(self, tool, args):
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        if res.get("isError"):
            raise RuntimeError(json.dumps(res.get("content"), ensure_ascii=False)[:120])
        return res.get("structuredContent") or {}


def _load_cache():
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(cache):
    with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=0, sort_keys=True)


def same_code(a, b):
    """Model codes match, allowing a series name or colour suffix on one side
    ('50SSTYLEFAB32LOR6' vs 'FAB32LOR6')."""
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a))


def _find(mcp, brand, code, name):
    """BestPrice product whose title carries the same brand + model code. Tries
    our listing title first, then just brand + code (extra words such as a
    colour can make BestPrice's search come back empty)."""
    for i, q in enumerate((name, f"{brand} {code}")):
        if i:
            time.sleep(PAUSE)
        res = mcp.call("search_products", {"query": q[:400], "limit": 5})
        for p in res.get("products") or []:
            b, c = extract_model(p.get("title"))
            if same_code(c, code) and (not brand or not b or b == brand):
                return p
    return None


def _offers(mcp, bp_id, offer_count):
    seen, out = set(), []
    objectives = ["lowest_item_price"] + (["merchant_rating"] if (offer_count or 0) > 10 else [])
    for i, obj in enumerate(objectives):
        if i:
            time.sleep(PAUSE)
        res = mcp.call("compare_offers", {"product_id": bp_id, "objective": obj, "limit": 10})
        for o in res.get("offers") or []:
            if o.get("offer_id") in seen or not o.get("item_price"):
                continue
            seen.add(o.get("offer_id"))
            out.append([o.get("merchant_name"), o["item_price"], o.get("availability") or ""])
    return out


def run(models, push, log=print, budget_minutes=25):
    """models: [{'name': ...}] in priority order (yours first). push(items)
    sends a batch to /api/ingest/market. Stops at the time budget or after 5
    errors in a row."""
    t_end = time.time() + budget_minutes * 60
    cache = _load_cache()
    today = dt.date.today()
    targets, keys = [], set()
    for m in models:
        brand, code = extract_model(m.get("name"))
        if len(code) < 6 or (brand, code) in keys:     # too short to match safely
            continue
        keys.add((brand, code))
        targets.append((brand, code, m["name"]))
    log(f"[market] BestPrice: {len(targets)} models")
    try:
        mcp = Mcp()
    except Exception as e:
        log(f"   ! market: BestPrice unreachable ({str(e)[:60]}) - skipping")
        return
    batch, done, missing, fails = [], 0, 0, 0
    for brand, code, name in targets:
        if time.time() > t_end:
            log(f"   market: time budget reached after {done} models")
            break
        if fails >= 5:
            log("   ! market: 5 errors in a row - stopping for today")
            break
        key = f"{brand}|{code}"
        hit = cache.get(key)
        try:
            if hit is None or (not hit.get("id") and
                               (today - dt.date.fromisoformat(hit.get("checked", "2000-01-01"))).days >= RETRY_MISSES_DAYS):
                p = _find(mcp, brand, code, name)
                time.sleep(PAUSE)
                hit = cache[key] = ({"id": p["product_id"], "title": p.get("title"), "checked": today.isoformat()}
                                    if p else {"id": None, "checked": today.isoformat()})
                if p:
                    hit["count"] = p.get("offer_count")
            if not hit.get("id"):
                missing += 1
                continue
            offers = _offers(mcp, hit["id"], hit.get("count") or 11)
            time.sleep(PAUSE)
            fails = 0
        except Exception as e:
            fails += 1
            log(f"   ! market {code}: {str(e)[:70]}")
            time.sleep(30 * fails)      # back off; BestPrice errors on heavy use
            continue
        done += 1
        if offers:
            batch.append({"brand": brand, "code": code, "bp_id": hit["id"], "title": hit.get("title"),
                          "offer_count": hit.get("count"), "offers": offers})
        if len(batch) >= 25:
            push(batch)
            batch = []
            _save_cache(cache)
        if done % 100 == 0:
            log(f"   market: {done}/{len(targets)} models read")
    if batch:
        push(batch)
    _save_cache(cache)
    log(f"[market] DONE: {done} models read, {missing} not on BestPrice")
