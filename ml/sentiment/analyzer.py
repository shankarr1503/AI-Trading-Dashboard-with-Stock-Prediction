"""
Financial news sentiment.

FinBERT (ProsusAI/finbert) when `transformers` is installed, otherwise a
whole-word financial lexicon. Headlines come from NewsAPI (if NEWS_API_KEY is
set) or Yahoo Finance. If no headlines can be fetched the result is NEUTRAL
with `headlines_analyzed = 0` — the service never invents headlines.
"""
import logging
import os
import threading
from typing import Any, Dict, List

import httpx
import yfinance as yf

from shared import sentiment_lexicon

logger = logging.getLogger(__name__)

try:
    from transformers import pipeline
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logger.info("transformers not installed; using lexicon sentiment")


class SentimentAnalyzer:
    def __init__(self, model_name: str = os.environ.get("FINBERT_MODEL", "ProsusAI/finbert")):
        self.model_name = model_name
        self._pipeline = None
        self._load_lock = threading.Lock()
        self._load_failed = False

    def _load_model(self) -> None:
        if self._pipeline is not None or self._load_failed or not TRANSFORMERS_AVAILABLE:
            return
        with self._load_lock:
            if self._pipeline is not None:
                return
            try:
                logger.info("Loading FinBERT model %s", self.model_name)
                self._pipeline = pipeline("text-classification", model=self.model_name, tokenizer=self.model_name,
                                          truncation=True, max_length=512, top_k=None)
            except Exception as e:
                logger.error("Failed to load FinBERT: %s", e)
                self._load_failed = True

    @property
    def method(self) -> str:
        return "finbert" if self._pipeline is not None else "lexicon"

    def analyze_text(self, text: str) -> Dict[str, float]:
        self._load_model()
        if self._pipeline is not None:
            try:
                result = self._pipeline(text[:2000])
                scores = {r["label"].lower(): float(r["score"]) for r in result[0]}
                return {k: round(scores.get(k, 0.0), 4) for k in ("positive", "negative", "neutral")}
            except Exception as e:
                logger.warning("FinBERT inference failed: %s", e)
        return sentiment_lexicon.score_text(text)

    def _fetch_news(self, symbol: str) -> List[str]:
        key = os.environ.get("NEWS_API_KEY", "")
        if key:
            try:
                resp = httpx.get(
                    "https://newsapi.org/v2/everything",
                    params={"q": symbol.split(".")[0], "language": "en", "sortBy": "publishedAt", "pageSize": 20},
                    headers={"X-Api-Key": key},
                    timeout=10,
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
                headlines = [f"{a.get('title') or ''}. {a.get('description') or ''}".strip() for a in articles if a.get("title")]
                if headlines:
                    return headlines
            except Exception as e:
                logger.warning("NewsAPI fetch failed for %s: %s", symbol, e)
        try:
            items = yf.Ticker(symbol).news or []
            out = []
            for item in items[:20]:
                content = item.get("content") if isinstance(item.get("content"), dict) else item
                title = content.get("title") or ""
                summary = content.get("summary") or content.get("description") or ""
                if title:
                    out.append(f"{title}. {summary}".strip())
            return out
        except Exception as e:
            logger.warning("Yahoo news fetch failed for %s: %s", symbol, e)
            return []

    def fetch_and_analyze(self, symbol: str) -> Dict[str, Any]:
        headlines = self._fetch_news(symbol)
        agg = sentiment_lexicon.aggregate([self.analyze_text(h) for h in headlines])
        return {
            "symbol": symbol,
            **agg,
            "headlines": headlines[:5],
            "headlines_analyzed": len(headlines),
            "source": self.method if headlines else "unavailable",
        }


sentiment_analyzer = SentimentAnalyzer()
