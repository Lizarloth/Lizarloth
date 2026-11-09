#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cooking.py  -  Pricedge: shared normalisation for the Cooking department.

The three retailers describe cooking appliances with different labels and
vocabularies (Public topSpecs, Kotsovolos attributes[], Plaisio card text).
Each fetcher collects whatever raw text it has, then calls cooking_fields()
here so every row lands with the SAME canonical tokens:

  hob_type   induction | ceramic | gas | mixed | enamel      (cookers, hobs)
  fuel       electric | gas | mixed                           (cookers)
  oven_lt    "71"                                             (cookers, ovens, microwaves)
  zones      "4"                                              (cookers, hobs)
  cleaning   pyrolytic,hydrolytic,catalytic,steam | none      (cookers, ovens)
  hood_type  chimney | slide-out | integrated | island | wall | downdraft | ceiling
  airflow    "730"  (m3/h)                                    (hoods)
  power_w    "900"                                            (microwaves, hobs)
  install    built-in | freestanding                          (microwaves)
  grill      yes                                              (microwaves)
  width_cm   "60"
  energy     A+ ...                                           (Latin letters)

Canonical subcategories: cooker, oven, hob, hood, microwave.
"""

import re
import unicodedata

COOKING_SUBCATS = ("cooker", "oven", "hob", "hood", "microwave")

COOKING_COLS = ["site", "category", "department", "sku_id", "brand", "name",
                "price", "list_price", "energy", "hob_type", "fuel", "oven_lt",
                "zones", "cleaning", "hood_type", "airflow", "power_w",
                "install", "grill", "width_cm", "color", "dimensions",
                "tech_tags", "program_tags", "image", "url"]


def fold(s):
    """Uppercase + strip accents so Greek cues match regardless of tonos."""
    s = unicodedata.normalize("NFD", str(s or "").upper())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _first(rules, text):
    t = fold(text)
    for token, cues in rules:
        if any(c in t for c in cues):
            return token
    return ""


# order matters: most specific first ("Κεραμική Επαγωγική" is induction,
# "Καμινάδα νησίδα" is an island hood, "Μεικτή" beats "Αερίου")
_HOB = [
    ("induction", ["ΕΠΑΓΩΓ", "INDUCTION"]),
    ("mixed",     ["ΜΕΙΚΤ", "ΜΙΚΤ", "MIXED", "COMBINED"]),
    ("ceramic",   ["ΚΕΡΑΜ", "CERAMIC", "VITRO"]),
    ("gas",       ["ΑΕΡΙ", "ΥΓΡΑΕΡ", "GAS"]),
    ("enamel",    ["ΕΜΑΓΙΕ", "ΕΜΑΓΙΈ", "ENAMEL", "ΑΝΟΞΕΙΔΩΤ", "ΜΑΝΤΕΜ", "SOLID"]),
]
_HOOD = [
    ("island",     ["ΝΗΣΙΔ", "ISLAND"]),
    ("downdraft",  ["ΠΑΓΚΟΥ", "DOWNDRAFT"]),
    ("ceiling",    ["ΟΡΟΦ", "CEILING"]),
    ("chimney",    ["ΤΖΑΚΙ", "ΚΑΜΙΝΑΔ", "CHIMNEY"]),
    ("slide-out",  ["ΣΥΡΟΜΕΝ", "ΤΗΛΕΣΚΟΠ", "TELESCOP", "ΠΤΥΣΣΟΜΕΝ", "SLIDE"]),
    ("integrated", ["ΜΗΧΑΝΙΣΜ", "ΕΝΤΟΙΧΙΖ", "INTEGRATED", "BUILT IN"]),
    ("wall",       ["ΕΛΕΥΘΕΡ", "ΤΟΙΧΟΥ", "WALL", "FREE"]),
]
_CLEAN = [
    ("pyrolytic",  ["ΠΥΡΟΛ", "PYRO"]),
    ("hydrolytic", ["ΥΔΡΟΛ", "AQUA", "HYDRO"]),
    ("catalytic",  ["ΚΑΤΑΛΥΤ", "CATALY"]),
    ("steam",      ["ΑΤΜ", "STEAM"]),
]
_GR2LAT = str.maketrans({"Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z"})

# accessory rows that retailers list inside the hood/cooker categories
_ACCESSORY = re.compile(r"ΑΞΕΣΟΥΑΡ|ΦΙΛΤΡ|ΜΕΤΟΠ|ΓΩΝΙΑ ΣΥΝΔ|ΕΠΕΚΤΑΣ|ΜΠΟΥΡΙ|ΣΩΛΗΝ|ΡΑΦΙ|"
                        r"ΣΕΤ |ΚΙΤ |KIT |ΚΑΛΥΜΜΑ|ΤΑΨΙ|ΣΧΑΡΑ|ΟΔΗΓ|ΣΥΝΔΕΤΙΚ|ΠΡΟΣΟΨ|"
                        r"ΠΛΑΙΣΙΟ ΧΑΜΗΛ|ΕΓΚΑΤΑΣΤΑΣΗ ΓΙΑ|ΑΝΑΚΥΚΛΩΣ")


def hob_type(text):
    return _first(_HOB, text)


def hood_type(text):
    return _first(_HOOD, text)


def fuel(text):
    """Cooker fuel: mixed (gas hob + electric oven), gas, else electric."""
    t = fold(text)
    if any(c in t for c in ("ΜΕΙΚΤ", "ΜΙΚΤ", "MIXED", "COMBINED")):
        return "mixed"
    if any(c in t for c in ("ΑΕΡΙ", "ΥΓΡΑΕΡ", "GAS")):
        return "gas"
    if t.strip():
        return "electric"
    return ""


def cleaning(text):
    t = fold(text)
    if not t.strip():
        return ""
    found = [tok for tok, cues in _CLEAN if any(c in t for c in cues)]
    if found:
        return ",".join(found)
    if "ΔΕ" in t and "ΔΙΑΘΕΤ" in t:
        return "none"
    return ""


def energy(text):
    m = re.search(r"\b([A-GΑΒΕΖ])(\+{0,3})", str(text or "").strip().upper())
    return (m.group(1).translate(_GR2LAT) + m.group(2)) if m else ""


def num(text, lo=None, hi=None):
    """First number in text ('1.270 W' -> 1270, '59,80 cm' -> 59.8)."""
    s = str(text or "")
    m = re.search(r"\d{1,3}(?:\.\d{3})+(?!\d)|\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    raw = m.group(0)
    v = float(raw.replace(".", "")) if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) else float(raw.replace(",", "."))
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def _fmt(v):
    return "" if v is None else (str(int(v)) if float(v).is_integer() else str(round(v, 1)))


def width_from(name="", dims="", dims_axis=0, explicit=""):
    """Appliance width in cm. Priority: an explicit width spec, a 'NN cm' in the
    name, then axis `dims_axis` of a 'a x b x c' dimension string. Values in mm
    (Kotsovolos sometimes) are scaled down."""
    v = num(explicit, 20, 1300)
    if v is None:
        m = re.search(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*cm\b", str(name), re.I)
        if m:
            v = num(m.group(1), 20, 130)
    if v is None and dims:
        parts = re.split(r"\s*[xX×]\s*", str(dims))
        if len(parts) >= 3:
            v = num(parts[dims_axis])
    if v is not None and v > 130:          # millimetres
        v = v / 10
    if v is None or not (25 <= v <= 130):
        return ""
    return _fmt(round(v))


def is_accessory(name, *extra):
    return bool(_ACCESSORY.search(fold(" ".join([str(name or "")] + [str(x or "") for x in extra]))))


def keep(cat, row):
    """False for accessory rows retailers mix into the appliance categories
    (hood frames, recirculation kits, front panels, oven trays). A real hood
    always has a type; other appliances survive unless named as an accessory
    AND missing their core spec."""
    if cat == "hood":
        return bool(row.get("hood_type")) and not is_accessory(row.get("name"))
    core = {"cooker": "hob_type", "oven": "oven_lt", "hob": "hob_type",
            "microwave": "oven_lt"}.get(cat)
    return not (is_accessory(row.get("name")) and not row.get(core))


_COVER = {
    "cooker":    ("fuel", "hob_type", "oven_lt", "zones", "cleaning", "energy", "width_cm"),
    "oven":      ("oven_lt", "cleaning", "energy", "width_cm"),
    "hob":       ("hob_type", "zones", "width_cm", "power_w"),
    "hood":      ("hood_type", "airflow", "energy", "width_cm"),
    "microwave": ("oven_lt", "power_w", "install", "grill"),
}


def print_coverage(rows):
    """Per-subcategory fill rate of each canonical field (inspection runs)."""
    for cat, keys in _COVER.items():
        rs = [r for r in rows if r.get("category") == cat]
        if not rs:
            continue
        fill = ", ".join(f"{k} {sum(1 for r in rs if r.get(k))}" for k in keys)
        print(f"  {cat:10} {len(rs):4d} rows · {fill}")


def cooking_fields(cat, name="", *, type_text="", hob_text="", fuel_text="",
                   oven_lt="", zones="", clean_text="", hood_text="",
                   airflow="", power="", energy_text="", color="",
                   dimensions="", dims_axis=0, width="", grill_text="",
                   install_text=""):
    """Build the canonical cooking fields for one product. Every argument is raw
    retailer text; anything missing stays '' (never guessed)."""
    blob = f"{name} {type_text}"
    out = {"department": "cooking", "energy": energy(energy_text),
           "color": color or "", "dimensions": dimensions or ""}
    lt = num(oven_lt, 5, 200)
    if lt is None:
        m = re.search(r"(\d{2,3})\s*(?:lt|l|λίτρ)\b", str(name), re.I)
        lt = num(m.group(1), 5, 200) if m else None
    out["oven_lt"] = _fmt(lt) if cat in ("cooker", "oven", "microwave") else ""

    if cat in ("cooker", "hob"):
        out["hob_type"] = hob_type(hob_text) or hob_type(blob)
        # induction is a glass-ceramic surface, so retailers often file it as
        # "ceramic" — the name saying induction wins
        if out["hob_type"] == "ceramic" and any(c in fold(blob) for c in ("ΕΠΑΓΩΓ", "INDUCTION")):
            out["hob_type"] = "induction"
        z = num(zones, 1, 12)
        out["zones"] = _fmt(z)
    if cat == "cooker":
        out["fuel"] = fuel(fuel_text) or fuel(blob) or ("gas" if out.get("hob_type") == "gas" else "")
        if out["fuel"] == "electric" and out.get("hob_type") == "gas":
            out["fuel"] = "gas"
        # gas/mixed cookers list a stainless 'Ανοξείδωτη' surface, which is not
        # an enamel plate — the hob follows the fuel there
        if out["fuel"] in ("gas", "mixed"):
            out["hob_type"] = out["fuel"]
    if cat in ("cooker", "oven"):
        out["cleaning"] = cleaning(clean_text)
    if cat == "hood":
        out["hood_type"] = hood_type(hood_text) or hood_type(blob)
        out["airflow"] = _fmt(num(airflow, 100, 2500))
    if cat in ("microwave", "hob"):
        out["power_w"] = _fmt(num(power, 300, 15000))
    if cat == "microwave":
        t = fold(f"{install_text} {name}")
        out["install"] = "built-in" if ("ΕΝΤΟΙΧ" in t or "BUILT" in t) else "freestanding"
        g = fold(f"{grill_text} {name}")
        out["grill"] = "yes" if ("GRILL" in g and "ΔΕΝ ΔΙΑΘ" not in fold(grill_text)) else ""
    out["width_cm"] = "" if cat == "microwave" else width_from(name, dimensions, dims_axis, width)
    return out
