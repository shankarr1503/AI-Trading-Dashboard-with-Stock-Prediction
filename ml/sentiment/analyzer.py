"""
FinBERT Sentiment Analyzer for Financial News.
Uses HuggingFace ProsusAI/finbert model to classify financial text as
Positive / Negative / Neutral with confidence scores.
Falls back to keyword-based scoring when transformers unavailable.
"""
import logging
import re
from typing import Dict, Any, List
import httpx

try:
    from backend.config import settings
except ImportError:
    settings = None

logger = logging.getLogger(__name__)

try:
    from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logger.warning("transformers library not available. Using keyword-based fallback.")

# Positive / negative financial keywords for fallback
POSITIVE_KEYWORDS = [
    "surge", "rally", "gain", "rise", "growth", "profit", "beat", "record",
    "upgrade", "buy", "bullish", "outperform", "strong", "up", "high",
    "positive", "revenue", "earnings beat", "dividend", "acquisition",
]
NEGATIVE_KEYWORDS = [
    "drop", "fall", "loss", "decline", "down", "sell", "bearish", "miss",
    "underperform", "downgrade", "weak", "cut", "layoff", "lawsuit",
    "recession", "crash", "plunge", "warning", "risk", "debt",
]


class SentimentAnalyzer:
    """FinBERT-based financial sentiment classifier."""

    def __init__(self, model_name: str = "ProsusAI/finbert"):
        self.model_name = model_name
        self._pipeline = None
        self._model_loaded = False

    def _load_model(self):
        """Lazy-load the FinBERT pipeline (downloads ~400MB on first use)."""
        if self._model_loaded:
            return
        if not TRANSFORMERS_AVAILABLE:
            return
        try:
            logger.info(f"Loading FinBERT model: {self.model_name}")
            self._pipeline = pipeline(
                "text-classification",
                model=self.model_name,
                tokenizer=self.model_name,
                max_length=512,
                truncation=True,
                top_k=None,  # return all labels
            )
            self._model_loaded = True
            logger.info("FinBERT loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load FinBERT: {e}")

    def analyze_text(self, text: str) -> Dict[str, float]:
        """
        Analyze a single text snippet. Returns positive/negative/neutral scores.
        """
        self._load_model()
        if self._model_loaded and self._pipeline:
            return self._finbert_score(text)
        return self._keyword_score(text)

    def _finbert_score(self, text: str) -> Dict[str, float]:
        """Run FinBERT inference and return probability scores."""
        try:
            results = self._pipeline(text[:512])
            scores = {r["label"].lower(): r["score"] for r in results[0]}
            return {
                "positive": round(scores.get("positive", 0), 4),
                "negative": round(scores.get("negative", 0), 4),
                "neutral": round(scores.get("neutral", 0), 4),
                "method": "finbert",
            }
        except Exception as e:
            logger.warning(f"FinBERT inference failed: {e}")
            return self._keyword_score(text)

    def _keyword_score(self, text: str) -> Dict[str, float]:
        """Simple keyword-based fallback scorer."""
        text_lower = text.lower()
        pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
        neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)
        total = pos_count + neg_count + 1  # +1 to avoid division by zero

        positive = pos_count / total
        negative = neg_count / total
        neutral = 1.0 - positive - negative

        return {
            "positive": round(positive, 4),
            "negative": round(negative, 4),
            "neutral": round(max(0, neutral), 4),
            "method": "keyword_fallback",
        }

    def analyze_headlines(self, headlines: List[str]) -> Dict[str, Any]:
        """Analyze a list of headlines and return aggregate sentiment."""
        if not headlines:
            return {
                "positive": 0.33, "negative": 0.33, "neutral": 0.34,
                "overall": "NEUTRAL", "compound_score": 0.0,
            }

        scores = [self.analyze_text(h) for h in headlines]
        avg_pos = sum(s["positive"] for s in scores) / len(scores)
        avg_neg = sum(s["negative"] for s in scores) / len(scores)
        avg_neu = sum(s["neutral"] for s in scores) / len(scores)

        compound = avg_pos - avg_neg  # -1 to +1

        if compound > 0.15:
            overall = "BULLISH"
        elif compound < -0.15:
            overall = "BEARISH"
        else:
            overall = "NEUTRAL"

        return {
            "positive": round(avg_pos, 4),
            "negative": round(avg_neg, 4),
            "neutral": round(avg_neu, 4),
            "overall": overall,
            "compound_score": round(compound, 4),
            "headlines_analyzed": len(headlines),
        }

    async def fetch_and_analyze(self, symbol: str) -> Dict[str, Any]:
        """Fetch news headlines for a symbol and analyze sentiment."""
        headlines = await self._fetch_news(symbol)
        result = self.analyze_headlines(headlines)
        result["symbol"] = symbol
        result["headlines"] = headlines[:5]  # return sample headlines
        result["source"] = "finbert" if self._model_loaded else "keyword_fallback"
        return result

    async def _fetch_news(self, symbol: str) -> List[str]:
        """Fetch financial news headlines via NewsAPI or fallback."""
        try:
            import os
            news_key = os.environ.get("NEWS_API_KEY", "")
            if news_key:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://newsapi.org/v2/everything",
                        params={
                            "q": symbol,
                            "language": "en",
                            "sortBy": "publishedAt",
                            "pageSize": 20,
                            "apiKey": news_key,
                        },
                    )
                    articles = resp.json().get("articles", [])
                    return [a.get("title", "") + " " + (a.get("description") or "") for a in articles]
        except Exception as e:
            logger.warning(f"News fetch failed for {symbol}: {e}")

        # Fallback: generic positive/negative headlines
        return [
            f"{symbol} reports strong quarterly earnings",
            f"Analyst upgrades {symbol} to buy rating",
            f"Market uncertainty weighs on {symbol} shares",
        ]


sentiment_analyzer = SentimentAnalyzer()
