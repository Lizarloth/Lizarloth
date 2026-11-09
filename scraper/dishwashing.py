#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dishwashing.py  -  Pricedge: shared normalisation for the Dishwashing department.

Same idea as cooking.py: every fetcher collects its retailer's raw text and
calls dish_fields() so all rows carry the same canonical tokens. No retailer
splits dishwashers the way the dashboard does, so the subcategory itself is
derived per product from install type, width and size:

  dw_free_60   freestanding, 60 cm          dw_bi_60   built-in, 60 cm
  dw_free_45   freestanding, 45 cm (slim)   dw_bi_45   built-in, 45 cm (slim)
  dw_compact   tabletop / mini

Canonical fields:
  install      freestanding | built-in | compact
  integration  fully | semi                 (built-in only, when stated)
  settings     "14"   place settings (σερβίτσια)
  width_cm     "60"
  energy       A..G (Latin letters)
  noise_db     "44"
  programs     "8"
  wifi / half_load / autodose   yes        (only on positive evidence)
"""

import re

from cooking import fold, num, energy, width_from, is_accessory, _fmt

DISH_SUBCATS = ("dw_free_60", "dw_free_45", "dw_bi_60", "dw_bi_45", "dw_compact")

DISH_COLS = ["site", "category", "department", "sku_id", "brand", "name",
             "price", "list_price", "install", "integration", "settings",
             "width_cm", "energy", "noise_db", "programs", "wifi", "half_load",
             "autodose", "color", "dimensions", "tech_tags", "program_tags", "image", "url"]

_YES = re.compile(r"ΔΙΑΘΕΤ|ΥΠΟΣΤΗΡΙΖ|^ΝΑΙ|YES|WIFI|WI-FI|BLUETOOTH")
_NO = re.compile(r"ΔΕΝ |ΔΕ ΔΙΑΘ|^ΟΧΙ|^NO\b")


def _yes(text):
    t = fold(text).strip()
    return "yes" if t and _YES.search(t) and not _NO.search(t) else ""


def dish_type(install, width, settings, height=None):
    """Five dashboard types from install + width (+ size for compacts)."""
    if install == "compact":
        return "dw_compact"
    w = width
    if w is None and settings is not None:
        w = 45 if settings <= 11 else 60          # slimlines hold 9-11 settings
    slim = w is not None and w <= 50
    if install == "built-in":
        return "dw_bi_45" if slim else "dw_bi_60"
    return "dw_free_45" if slim else "dw_free_60"


def dish_fields(name="", *, install_text="", builtin_hint=False, settings="",
                width="", dimensions="", dims_axis=0, energy_text="", noise="",
                programs="", wifi_text="", half_text="", autodose_text="",
                semi_text="", color="", compact_text=""):
    """Canonical dishwasher fields for one product. All inputs are raw retailer
    text; anything missing stays '' rather than guessed."""
    blob = fold(f"{name} {install_text} {compact_text}")
    st = num(settings, 2, 20)
    if st is None:
        m = re.search(r"(\d{1,2})\s*ΣΕΡΒΙΤΣ", blob)
        st = num(m.group(1), 2, 20) if m else None

    # height from the dimension string catches tabletops that aren't labelled
    height = None
    if dimensions:
        parts = [num(p) for p in re.split(r"\s*[xX×]\s*", str(dimensions))]
        if len(parts) >= 3 and all(p is not None for p in parts[:3]):
            height = parts[2] if dims_axis == 0 else parts[0]   # W×D×H vs H×W×D
            if height and height > 130:
                height /= 10

    # tabletop wording only — "Κάτω Πάγκου" (under-counter) is a full-size model
    tabletop = (re.search(r"(ΑΝΩ|ΜΙΚΡΟ-?|ΕΠΙ) ?ΠΑΓΚΟΥ|ΕΠΙΤΡΑΠΕΖ|ΜΙΝΙ|\bMINI\b|COMPACT|TABLE ?TOP", blob)
                is not None)
    # a built-in machine is never a tabletop; dimension order on built-in
    # listings is unreliable, so only the wording can make one compact
    compact = ((tabletop or (not builtin_hint and ((st is not None and st <= 8)
                                                    or (height is not None and height < 65))))
               and not (st is not None and st >= 10))
    if compact:
        install = "compact"
    elif builtin_hint or "ΕΝΤΟΙΧ" in blob or "BUILT" in blob:
        install = "built-in"
    else:
        install = "freestanding"

    integration = ""
    if install == "built-in":
        if "ΠΛΗΡΩΣ" in blob or "FULLY" in blob:
            integration = "fully"
        elif "ΗΜΙ" in blob or "SEMI" in blob or _yes(semi_text):
            integration = "semi"

    w = width_from(name, dimensions, dims_axis, width)
    if not w:
        m = re.search(r"\b(45|60)\s*CM\b", blob) or re.search(r"\((45|60)\s*CM\)", blob)
        w = m.group(1) if m else ""
    wv = num(w)

    nz = num(noise, 30, 70)
    pr = num(programs, 1, 30)
    return {
        "department": "dishwashing",
        "category": dish_type(install, wv, st, height),
        "install": install,
        "integration": integration,
        "settings": _fmt(st),
        "width_cm": w,
        "energy": energy(energy_text),
        "noise_db": _fmt(nz),
        "programs": _fmt(pr),
        "wifi": _yes(wifi_text) if fold(wifi_text).strip() else ("yes" if "WIFI" in blob or "WI-FI" in blob else ""),
        "half_load": _yes(half_text),
        "autodose": _yes(autodose_text),
        "color": color or "",
        "dimensions": dimensions or "",
    }


def keep(row):
    """Drop racks, cutlery baskets, door panels listed inside dishwasher pages."""
    return not (is_accessory(row.get("name")) and not row.get("settings"))


_COVER = ("install", "settings", "width_cm", "energy", "noise_db", "programs", "wifi")


def print_coverage(rows):
    for cat in DISH_SUBCATS:
        rs = [r for r in rows if r.get("category") == cat]
        if rs:
            fill = ", ".join(f"{k} {sum(1 for r in rs if r.get(k))}" for k in _COVER)
            print(f"  {cat:11} {len(rs):4d} rows · {fill}")
