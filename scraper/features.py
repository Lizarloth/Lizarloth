#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
features.py  -  Pricedge: technology + program tags per product.

Kotsovolos ships two kinds of feature data in its listing attributes:
  * yes/no flags      "Air Fry: Διαθέτει", "Aqua Stop: Διαθέτει", ...
  * free-text fields  "Επιπλέον: ThermoFlow ..., Γρήγορη προθέρμανση, ..."
                      "Λειτουργία Προγραμμάτων Ατμού: Αποστείρωση & Λείανση"
extract() turns both into two short lists of canonical English tags per
department, so "Aqua Stop", "AquaStop" and "Σύστημα Πολλαπλής Προστασίας
Νερού" all become "Water protection". Only vocabulary tags are emitted —
free text that matches nothing is ignored rather than shown raw.
"""

import re

from cooking import fold

# canonical tag -> cue regex over accent-free uppercase text (Greek + English)
_COMMON_TECH = [
    ("WiFi",           r"WI-?FI"),
    ("Bluetooth",      r"BLUETOOTH"),
    ("App control",    r"SMART ΛΕΙΤΟΥΡΓ|\bAPP\b|HOME ?CONNECT|THINQ|SMARTTHINGS|HOMEWHIZ|6TH SENSE LIVE"),
    ("Touch display",  r"ΟΘΟΝΗ ΑΦΗΣ|TOUCH ?(DISPLAY|SCREEN)"),
    ("Child lock",     r"ΑΣΦΑΛΕΙΑ ΚΛΕΙΔΩΜ|ΠΑΙΔΙΚΟ ΚΛΕΙΔΩΜ|CHILD ?LOCK"),
    ("Delay start",    r"ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ ΧΡΟΝΟΥ|DELAY ?START|ΚΑΘΥΣΤΕΡΗΣΗ ΕΝΑΡΞ"),
    ("Inverter",       r"INVERTER|ΙΝΒΕΡΤΕΡ|\bCIM\b|ECOSILENCE|DIRECT ?DRIVE"),
]
VOCAB = {
    "cooling": {
        "tech": _COMMON_TECH + [
            ("Ice maker",        r"ΠΑΓΟΜΗΧΑΝ|ICE ?MAKER|ΜΗΧΑΝΙΣΜΟΣ ΓΙΑ ΠΑΓΑΚΙ"),
            ("Water dispenser",  r"ΕΞΩΤΕΡΙΚΗ ΠΑΡΟΧΗ|DISPENSER ΝΕΡΟΥ|WATER ?DISPENSER"),
            ("Fresh zone",       r"ΣΥΝΤΗΡΗΣΗΣ ΚΡΕΑΤ|ΚΡΕΑΣ - ΨΑΡΙ|BIOFRESH|VITAFRESH|FRESH ?ZONE|0 ?°C|ΜΗΔΕΝΙΚΗΣ"),
            ("Antibacterial",    r"ΑΝΤΙΒΑΚΤΗΡ|ANTI-?BACTER"),
            ("Reversible door",  r"ΑΛΛΑΓΗΣ ΦΟΡΑΣ"),
            ("LED lighting",     r"\bLED\b"),
            ("Multi airflow",    r"MULTI ?AIR ?FLOW|MULTIFLOW"),
            ("Quick freeze",     r"SUPER ?FREEZE|QUICK ?FREEZE|FAST ?FREEZE|ΓΡΗΓΟΡΗ ΚΑΤΑΨ"),
            ("Quick cool",       r"SUPER ?COOL|QUICK ?COOL|FAST ?COOL"),
            ("Holiday mode",     r"HOLIDAY|VACATION|ΔΙΑΚΟΠ"),
            ("Door alarm",       r"ΣΥΝΑΓΕΡΜ|DOOR ?ALARM|ΗΧΗΤΙΚΗ ΕΙΔΟΠ"),
            ("Convertible zone", r"CONVERTIBLE|ΜΕΤΑΤΡΕΨΙΜ"),
            ("Auto temperature", r"ΑΥΤΟΜΑΤΗ ΡΥΘΜΙΣΗ ΘΕΡΜΟΚΡ"),
        ],
        "programs": [],
    },
    "laundry": {
        "tech": _COMMON_TECH + [
            ("Steam",            r"ΑΤΜΟΥ|ΑΤΜΟΣ|STEAM"),
            ("Auto-dose",        r"ΑΥΤΟΜΑΤΗ ΔΟΣΟΛΟΓ|AUTO ?DOS|I-?DOS|TWINDOS|AUTODOSING"),
            ("Auto-weighing",    r"ΑΥΤΟΜΑΤΟ ΖΥΓΙΣΜ|ACTIVEWATER|ΑΝΑΓΝΩΡΙΣΗ ΒΑΡΟΥΣ|SENSOR WASH"),
            ("Heat pump",        r"ΑΝΤΛΙΑ ΘΕΡΜΟΤ|HEAT ?PUMP"),
            ("Condenser",        r"ΣΥΜΠΥΚΝΩΣ"),
            ("Water protection", r"ΠΡΟΣΤΑΣΙΑΣ ΝΕΡΟΥ|AQUA ?STOP|AQUASTOP|WATERPROOF"),
            ("Hot + cold fill",  r"ΔΙΠΛΗ ΠΑΡΟΧΗ"),
            ("Add garment",      r"ADD ?WASH|ADD ?GARMENT|ADD ?ITEM|ΠΡΟΣΘΗΚΗ ΡΟΥΧ"),
        ],
        "programs": [
            ("Quick wash",       r"ΓΡΗΓΟΡΟ ΠΡΟΓΡΑΜΜΑ|ΓΡΗΓΟΡΟ ΠΛΥΣ|QUICK|RAPID|SPEED ?WASH|SUPER ?QUICK"),
            ("Steam refresh",    r"STEAM ?REFRESH|ΦΡΕΣΚΑΡΙΣΜ|REFRESH"),
            ("Hygiene / sanitize", r"ΑΠΟΣΤΕΙΡΩΣ|HYGIENE|SANITI|ANTIBACTERIAL|ΑΝΤΙΒΑΚΤΗΡ|ALLERG|ΑΛΛΕΡΓ"),
            ("Easy iron",        r"EASY ?IRON|ΛΕΙΑΝΣ|ΣΙΔΕΡΩΜ|ΤΣΑΚΙΣ"),
            ("Baby care",        r"ΜΩΡΟΥ|BABY"),
            ("Sportswear",       r"ΑΘΛΗΤΙΚ|SPORT"),
            ("Wool",             r"ΜΑΛΛΙΝ|WOOL"),
            ("Delicates",        r"ΕΥΑΙΣΘ|DELICATE|ΜΕΤΑΞ|SILK"),
            ("Eco 40-60",        r"ECO ?40|ECO ?60|\bECO\b"),
            ("Rinse +",          r"ΞΕΒΓΑΛΜ|RINSE"),
            ("Pre-wash",         r"ΠΡΟΠΛΥΣ|PRE-?WASH"),
            ("Mixed load",       r"\bMIX\b|ΑΝΑΜΕΙΚΤ|ΜΙΚΤΑ"),
            ("Duvet",            r"ΠΑΠΛΩΜ|DUVET|ΚΟΥΒΕΡΤ"),
            ("Shirts",           r"ΠΟΥΚΑΜΙΣ|SHIRT"),
            ("Jeans",            r"JEANS|ΤΖΙΝ"),
        ],
    },
    "cooking": {
        "tech": _COMMON_TECH + [
            ("Pyrolytic",          r"ΠΥΡΟΛΥΣ|PYRO"),
            ("Hydrolytic",         r"ΥΔΡΟΛΥΣ|AQUA ?CLEAN|HYDRO"),
            ("Catalytic",          r"ΚΑΤΑΛΥΤ|CATALY"),
            ("Steam",              r"ΑΤΜΟΥ|ΑΤΜΟΣ|STEAM"),
            ("Air fry",            r"AIR ?FRY|AIRFRY"),
            ("Food probe",         r"ΑΙΣΘΗΤΗΡΑΣ ΘΕΡΜΟΚΡΑΣΙΑΣ ΦΑΓΗΤ|ΘΕΡΜΟΜΕΤΡ|PROBE"),
            ("Telescopic rails",   r"ΤΗΛΕΣΚΟΠ|TELESCOP"),
            ("Soft close",         r"SOFT ?CLOSE"),
            ("Rapid preheat",      r"ΓΡΗΓΟΡΗ ΠΡΟΘΕΡΜ|FAST ?PREHEAT|RAPID ?HEAT|QUICK ?HEAT"),
            ("Multi-level cooking", r"ΕΠΙΠΕΔΑ|THERMOFLOW|3D ?HOT|MULTILEVEL|SURROUNDCOOK|4D"),
            ("Residual heat indicator", r"ΥΠΟΛΟΙΠΟΥ ΘΕΡΜΟΤ"),
            ("Booster",            r"BOOST|ΤΑΧΕΙΑΣ ΘΕΡΜ|ΥΠΕΡΤΑΧΕΙΑΣ"),
            ("Flex zone",          r"FLEX ?ZONE|FLEXI|BRIDGE|COMBI ?ZONE"),
            ("Auto shut-off",      r"ΑΥΤΟΜΑΤΗ ΑΠΕΝΕΡΓ"),
            ("Filter saturation indicator", r"ΚΟΡΕΣΜΟΥ"),
            ("Microwave function", r"ΛΕΙΤΟΥΡΓΙΑ ΜΙΚΡΟΚΥΜ"),
            ("Deep tray",          r"ΒΑΘΥ ΤΑΨΙ"),
        ],
        "programs": [
            ("Defrost",            r"ΑΠΟΨΥΞ|DEFROST"),
            ("Pizza",              r"ΠΙΤΣ|PIZZA"),
            ("Grill",              r"GRILL|ΓΚΡΙΛ"),
            ("Hot air",            r"ΘΕΡΜΟΣ ΑΕΡΑΣ|ΘΕΡΜΟ ΑΕΡΑ|HOT ?AIR|ΑΕΡΟΘΕΡΜ|ΑΝΕΜΙΣΤΗΡ"),
            ("Bread / dough",      r"ΖΥΜΗ|ΨΩΜΙ|BREAD|DOUGH|ΦΟΥΣΚΩΜ"),
            ("Keep warm",          r"ΔΙΑΤΗΡΗΣΗ ΘΕΡΜ|KEEP ?WARM|ΖΕΣΤΑΜΑΤΟΣ"),
            ("Slow cook",          r"SLOW ?COOK|ΑΡΓΟ ΜΑΓΕΙΡ"),
            ("Eco",                r"\bECO\b"),
        ],
    },
    "dishwashing": {
        "tech": _COMMON_TECH + [
            ("Detergent detection", r"ΑΥΤΟΜΑΤΗ ΑΝΑΓΝΩΡΙΣΗ ΑΠΟΡΡΥΠ"),
            ("Auto-dose",          r"ΑΥΤΟΜΑΤΗ ΔΟΣΟΛΟΓ|AUTO ?DOS"),
            ("Aqua Stop",          r"AQUA ?STOP"),
            ("Aqua Sensor",        r"AQUA ?SENSOR"),
            ("Half load",          r"ΜΙΣΗΣ ΠΛΥΣΗΣ|HALF ?LOAD|ΜΙΣΟ ΦΟΡΤΙΟ"),
            ("Time remaining display", r"ΥΠΟΛΟΙΠΟΜΕΝΟΥ ΧΡΟΝΟΥ"),
            ("Auto-open drying",   r"AUTO ?OPEN|DOOR ?OPEN|AUTODOOR|EXTRA ?DRY|ΑΥΤΟΜΑΤΟ ΑΝΟΙΓΜΑ"),
            ("Zeolite drying",     r"ZEOLIT|ΖΕΟΛΙΘ"),
            ("Third rack",         r"3RD ?RACK|THIRD ?RACK|ΘΗΚΗ ΓΙΑ ΜΑΧΑΙΡΟΠ|ΣΥΡΤΑΡΙ ΜΑΧΑΙΡΟΠ|ΤΡΙΤΟ ΚΑΛΑΘΙ|3 ΕΠΙΠΕΔ"),
        ],
        "programs": [
            ("Eco",                r"\bECO\b"),
            ("Quick",              r"ΣΥΝΤΟΜΑ ΠΡΟΓΡΑΜΜ|ΓΡΗΓΟΡΟ|QUICK|RAPID|SPEED"),
            ("Steam / hygiene",    r"ΑΤΜΟΥ|STEAM|SANITI|HYGIENE"),
            ("Intensive",          r"INTENSIVE|ΕΝΤΑΤΙΚ|POWER ?CLEAN"),
            ("Glass",              r"GLASS|ΓΥΑΛΙΝ|ΚΡΥΣΤΑΛ"),
            ("Pre-rinse",          r"ΠΡΟΠΛΥΣ|ΜΟΥΛΙΑΣΜ|PRE-?RINSE|SOAK"),
            ("Auto",               r"ΑΥΤΟΜΑΤΟ ΠΡΟΓΡ|AUTO ?PROGRAM|\bAUTO\b"),
            ("Machine care",       r"MACHINE ?CARE"),
            ("Silent",             r"ΑΘΟΡΥΒ|SILENT|QUIET|NIGHT"),
        ],
    },
}
_COMPILED = {d: {k: [(t, re.compile(rx)) for t, rx in v[k]] for k in ("tech", "programs")}
             for d, v in VOCAB.items()}

_POSITIVE = re.compile(r"^(ΔΙΑΘΕΤΕΙ|ΥΠΟΣΤΗΡΙΖΕΤΑΙ|ΝΑΙ|YES|TRUE)\b")
_NEGATIVE = re.compile(r"^(ΔΕΝ|ΔΕ |ΟΧΙ|NO\b|FALSE|-$)")
# free-text attributes that describe features or programs
_TEXT_KEY = re.compile(r"ΕΠΙΠΛΕΟΝ|ΠΡΟΓΡΑΜΜ|ΛΕΙΤΟΥΡΓΙ|ΤΕΧΝΟΛΟΓ|ΧΑΡΑΚΤΗΡΙΣΤ|ΔΥΝΑΤΟΤΗΤ|ΣΥΝΔΕΣΙΜΟΤ|ΕΙΔΙΚ")
# ERP / logistics attributes that only look like features
_SKIP_KEY = re.compile(r"TOTAL SUPPORT|ΥΠΟΣΤΗΡΙΚΤΙΚΗ ΤΕΧΝΟΛΟΓΙΑ|ΕΥΡΟΣ |ΑΡΙΘΜΟΣ |ΑΤΟΚΟ|ΚΑΤΑΛΛΗΛΟ")


def feature_text(attrs):
    """One haystack from a Kotsovolos attribute map: the names of positive
    yes/no flags plus the values of free-text feature fields."""
    parts = []
    for k, v in (attrs or {}).items():
        fk, fv = fold(k), fold(v).strip()
        if _SKIP_KEY.search(fk) or not fv:
            continue
        if _POSITIVE.match(fv):
            # a bare "Συνδεσιμότητα: Διαθέτει" flag means the unit is connected
            parts.append("WIFI" if fk.strip() == "ΣΥΝΔΕΣΙΜΟΤΗΤΑ" else fk)
        elif _NEGATIVE.match(fv):
            continue
        elif _TEXT_KEY.search(fk):
            parts.append(f"{fk} {fv}")
    return " | ".join(parts)


def extract(attrs, dept, extra_text=""):
    """{'tech_tags': 'A|B', 'program_tags': 'C|D'} in vocabulary order for one
    product (named apart from the departments' numeric 'programs' count)."""
    voc = _COMPILED.get(dept)
    if not voc:
        return {"tech_tags": "", "program_tags": ""}
    hay = feature_text(attrs) + " | " + fold(extra_text)
    return {f"{'tech' if kind == 'tech' else 'program'}_tags":
            "|".join(t for t, rx in voc[kind] if rx.search(hay))
            for kind in ("tech", "programs")}
