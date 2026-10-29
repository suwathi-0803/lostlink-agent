"""Blind ownership verification.

The claimant's answers are compared against the item's hidden marks INSIDE
the MCP server. The conversational agent only ever receives a verdict and a
confidence band, never the hidden marks themselves, so it cannot leak them
even if a user tries to trick it.

Questions are generic per category and do not depend on what the hidden
marks actually say, so asking them reveals nothing.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

QUESTION_BANK = {
    "bottle": [
        "Describe any stickers, scratches, dents or markings on the bottle.",
        "What colour is the lid or cap, and what type of lid is it?",
        "Roughly how big is it (capacity), and is there anything written on it?",
    ],
    "wallet": [
        "What is inside the wallet (cards, ID, cash amount, anything else)?",
        "Is there a name on any card inside? Whose?",
        "Describe any wear, damage or distinctive design on it.",
    ],
    "id_card": [
        "Whose name and register number are on the ID card?",
        "What is attached to it (lanyard, holder, colour)?",
    ],
    "keys": [
        "How many keys are there and what do they open?",
        "Describe the keychain or anything attached to the ring.",
    ],
    "phone": [
        "What is the lock-screen or wallpaper image?",
        "Describe the case or cover and any cracks or stickers.",
        "What is the exact model and colour?",
    ],
    "laptop": [
        "Describe any stickers, scratches or labels on the laptop.",
        "What is the exact model and what name is on the login screen?",
    ],
    "charger": [
        "Describe any markings, tape or names on the charger or cable.",
        "What wattage or model is it, and is the cable damaged anywhere?",
    ],
    "earphones": [
        "Describe the case and any scratches, stickers or damage.",
        "Which model are they and is anything missing (ear tips, one bud)?",
    ],
    "bag": [
        "List the main things inside the bag.",
        "Describe any keychains, patches, damage or names on it.",
    ],
}
DEFAULT_QUESTIONS = [
    "Describe any marks, damage, stickers or names on the item that only the owner would know.",
    "What was inside it or attached to it, if anything?",
    "Where and when exactly did you lose it?",
]

STOPWORDS = set("""
a an the and or of on in at to with without near my is it its it's this that there
was were be been has have had i me mine for from by as some any one about around
inside side small big little very just also like has have which who what
""".split())

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "hundred": "100",
}

STRONG, PARTIAL = 0.6, 0.35
MAX_ATTEMPTS = 2


def questions_for(category: str) -> list[str]:
    return QUESTION_BANK.get(category, DEFAULT_QUESTIONS)


def _tokens(text: str) -> set[str]:
    text = (text or "").lower().replace("₹", " rs ").replace("rupees", "rs").replace("rupee", "rs")
    words = re.findall(r"[a-z0-9]+", text)
    out = set()
    for w in words:
        w = NUMBER_WORDS.get(w, w)
        if w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def score_mark(mark: str, answer: str) -> float:
    """How well one hidden mark is covered by the answer text (0..1)."""
    mt, at = _tokens(mark), _tokens(answer)
    if not mt:
        return 0.0
    overlap = len(mt & at) / len(mt)
    partial = fuzz.partial_ratio(mark.lower(), answer.lower()) / 100
    return max(overlap, partial if partial >= 0.85 else 0.0)


def score_answers(marks: list[str], answer: str) -> float:
    marks = [m.strip() for m in marks if m.strip()]
    if not marks:
        return 0.0
    base = sum(score_mark(m, answer) for m in marks) / len(marks)
    # Anti "shotgun" guard: an answer that lists every possible feature is penalised.
    if len(_tokens(answer)) > 45:
        base *= 0.8
    return round(min(base, 1.0), 3)


def verdict_for(confidence: float | None) -> str:
    if confidence is None:
        return "manual_check"
    if confidence >= STRONG:
        return "strong_match"
    if confidence >= PARTIAL:
        return "partial_match"
    return "no_match"


def band(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    return "high" if confidence >= STRONG else "medium" if confidence >= PARTIAL else "low"
