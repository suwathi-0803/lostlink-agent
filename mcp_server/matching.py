"""Explainable lost <-> found matching.

The LLM normalises free text into structured fields at intake time
(category, colour, brand, location, date). Scoring here is deterministic,
so every match comes with human-readable reasons and the same inputs
always give the same score.

Weights (total 100):
    category 35 | colour 15 | brand 15 | location 15 | date 10 | description 10
"""
from __future__ import annotations

from datetime import date, datetime

from rapidfuzz import fuzz

CATEGORIES = [
    "bottle", "wallet", "id_card", "keys", "phone", "laptop", "charger",
    "earphones", "bag", "umbrella", "book", "calculator", "watch",
    "spectacles", "jewellery", "clothing", "other",
]

COLOR_GROUPS = {
    "blue": ["blue", "navy", "sky blue", "light blue", "dark blue", "teal", "cyan"],
    "black": ["black", "jet black", "matte black"],
    "white": ["white", "off white", "off-white", "cream", "ivory"],
    "grey": ["grey", "gray", "silver", "ash", "metallic", "steel"],
    "red": ["red", "maroon", "crimson", "wine"],
    "green": ["green", "olive", "mint", "lime"],
    "yellow": ["yellow", "mustard"],
    "gold": ["gold", "golden"],
    "brown": ["brown", "tan", "beige", "khaki", "coffee"],
    "pink": ["pink", "rose", "peach"],
    "purple": ["purple", "violet", "lavender"],
    "orange": ["orange"],
    "multicolor": ["multicolor", "multicolour", "multi", "printed"],
}

W_CATEGORY, W_COLOR, W_BRAND, W_LOCATION, W_DATE, W_DESC = 35, 15, 15, 15, 10, 10


def normalize_category(value: str) -> str:
    v = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "water_bottle": "bottle", "flask": "bottle", "sipper": "bottle", "tumbler": "bottle",
        "purse": "wallet", "idcard": "id_card", "id": "id_card", "student_id": "id_card",
        "key": "keys", "keychain": "keys", "mobile": "phone", "smartphone": "phone",
        "adapter": "charger", "cable": "charger", "power_bank": "charger",
        "earbuds": "earphones", "headphones": "earphones", "airpods": "earphones",
        "backpack": "bag", "handbag": "bag", "notebook": "book", "glasses": "spectacles",
        "specs": "spectacles", "ring": "jewellery", "chain": "jewellery", "jacket": "clothing",
        "hoodie": "clothing",
    }
    v = aliases.get(v, v)
    return v if v in CATEGORIES else "other"


def color_group(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    words = v.replace("-", " ").replace("/", " ").split()
    for group, names in COLOR_GROUPS.items():
        for name in names:
            if name == v or name in words or (" " in name and name in v):
                return group
    return v


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def score_pair(lost: dict, found: dict) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for how likely `found` is the item in `lost`."""
    reasons: list[str] = []
    cat_l, cat_f = normalize_category(lost["category"]), normalize_category(found["category"])
    if cat_l != cat_f and "other" not in (cat_l, cat_f):
        return 0, [f"different category ({cat_l} vs {cat_f})"]

    score = 0.0
    if cat_l == cat_f:
        score += W_CATEGORY
        reasons.append(f"same category ({cat_l})")
    else:
        score += W_CATEGORY * 0.4
        reasons.append("category uncertain (one report says 'other')")

    # colour
    cl, cf = color_group(lost.get("color", "")), color_group(found.get("color", ""))
    if cl and cf:
        if cl == cf:
            score += W_COLOR
            reasons.append(f"colour matches ({cl})")
        else:
            reasons.append(f"colour differs ({cl} vs {cf})")
    else:
        score += W_COLOR / 2
        reasons.append("colour not given in one report")

    # brand
    bl, bf = (lost.get("brand") or "").strip(), (found.get("brand") or "").strip()
    if bl and bf:
        if fuzz.ratio(bl.lower(), bf.lower()) >= 80:
            score += W_BRAND
            reasons.append(f"brand matches ({bf})")
        else:
            reasons.append(f"brand differs ({bl} vs {bf})")
    else:
        score += W_BRAND / 2
        reasons.append("brand not given in one report")

    # location
    ll, lf = (lost.get("location") or "").strip(), (found.get("location") or "").strip()
    if ll and lf:
        sim = fuzz.token_set_ratio(ll.lower(), lf.lower())
        if sim >= 80:
            score += W_LOCATION
            reasons.append(f"same place ({lf})")
        else:
            score += W_LOCATION * 0.25
            reasons.append(f"different place ({ll} vs {lf}) - items do get moved")
    else:
        score += W_LOCATION / 2

    # date: found should be on/after the day it was lost
    dl, df = _parse_date(lost.get("date_lost", "")), _parse_date(found.get("date_found", ""))
    if dl and df:
        delta = (df - dl).days
        if delta < -1:
            reasons.append("found BEFORE it was reported lost")
        elif delta <= 3:
            score += W_DATE
            reasons.append(f"found {max(delta, 0)} day(s) after loss")
        elif delta <= 7:
            score += W_DATE * 0.6
            reasons.append(f"found {delta} days after loss")
        elif delta <= 14:
            score += W_DATE * 0.3
            reasons.append(f"found {delta} days after loss")
        else:
            reasons.append(f"found {delta} days after loss (long gap)")
    else:
        score += W_DATE / 2

    # free-text description similarity
    desc_sim = fuzz.token_set_ratio(
        (lost.get("description") or "").lower(), (found.get("description") or "").lower()
    )
    score += W_DESC * desc_sim / 100
    if desc_sim >= 60:
        reasons.append("descriptions are similar")

    return round(score), reasons


def rank_matches(target: dict, candidates: list[dict], target_is_lost: bool,
                 min_score: int = 40, top_k: int = 5) -> list[dict]:
    ranked = []
    for cand in candidates:
        lost, found = (target, cand) if target_is_lost else (cand, target)
        s, reasons = score_pair(lost, found)
        if s >= min_score:
            ranked.append({"candidate": cand, "score": s, "reasons": reasons})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked[:top_k]
