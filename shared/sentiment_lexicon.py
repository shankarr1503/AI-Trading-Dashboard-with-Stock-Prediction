"""
Lightweight financial-news sentiment scorer used when FinBERT is unavailable.

Matches whole words/phrases only (the previous substring matching scored
"update" as positive because it contains "up").
"""
import re
from typing import Dict, List

POSITIVE = [
    "surge", "surges", "rally", "rallies", "gain", "gains", "rise", "rises", "growth",
    "profit", "profits", "beat", "beats", "record high", "upgrade", "upgraded", "bullish",
    "outperform", "strong", "raises guidance", "raised guidance", "dividend increase",
    "buyback", "tops estimates", "exceeds expectations", "soar", "soars", "jumps",
]
NEGATIVE = [
    "drop", "drops", "fall", "falls", "loss", "losses", "decline", "declines", "bearish",
    "miss", "misses", "underperform", "downgrade", "downgraded", "weak", "cut", "cuts",
    "layoff", "layoffs", "lawsuit", "probe", "recession", "crash", "plunge", "plunges",
    "warning", "lowers guidance", "lowered guidance", "slump", "slumps", "tumbles", "fraud",
]

_POS_RE = [re.compile(rf"\b{re.escape(w)}\b") for w in POSITIVE]
_NEG_RE = [re.compile(rf"\b{re.escape(w)}\b") for w in NEGATIVE]


def score_text(text: str) -> Dict[str, float]:
    t = text.lower()
    pos = sum(1 for r in _POS_RE if r.search(t))
    neg = sum(1 for r in _NEG_RE if r.search(t))
    total = pos + neg
    if total == 0:
        return {"positive": 0.0, "negative": 0.0, "neutral": 1.0}
    # Keep some neutral mass: one keyword is weak evidence.
    strength = total / (total + 1)
    positive = strength * pos / total
    negative = strength * neg / total
    return {"positive": round(positive, 4), "negative": round(negative, 4), "neutral": round(1 - positive - negative, 4)}


def aggregate(scores: List[Dict[str, float]]) -> Dict[str, float]:
    if not scores:
        return {"positive": 0.0, "negative": 0.0, "neutral": 1.0, "compound_score": 0.0, "overall": "NEUTRAL"}
    n = len(scores)
    pos = sum(s["positive"] for s in scores) / n
    neg = sum(s["negative"] for s in scores) / n
    neu = sum(s["neutral"] for s in scores) / n
    compound = pos - neg
    overall = "BULLISH" if compound > 0.15 else "BEARISH" if compound < -0.15 else "NEUTRAL"
    return {
        "positive": round(pos, 4),
        "negative": round(neg, 4),
        "neutral": round(neu, 4),
        "compound_score": round(compound, 4),
        "overall": overall,
    }
