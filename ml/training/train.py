"""
Train all ML models for one or more symbols and save them to disk.

Usage:
    python -m ml.training.train --symbol AAPL --period 5y
    python -m ml.training.train --symbol AAPL,MSFT,NVDA
"""
import argparse
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def train_symbol(symbol: str, period: str = "5y", model_dir: str = "ml/saved_models") -> dict:
    from ml.ensemble.predictor import EnsemblePredictor

    predictor = EnsemblePredictor(symbol.upper(), model_dir=model_dir)
    report = predictor.train_all(period)
    logger.info("Training report for %s: %s", symbol.upper(), json.dumps(report, default=str))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train ML models for stock symbols")
    parser.add_argument("--symbol", default="AAPL", help="Symbol or comma-separated symbols")
    parser.add_argument("--period", default="5y", help="History to train on (2y, 5y, 10y, max)")
    parser.add_argument("--model-dir", default="ml/saved_models")
    args = parser.parse_args()

    failed = False
    for sym in [s.strip() for s in args.symbol.split(",") if s.strip()]:
        try:
            train_symbol(sym, args.period, args.model_dir)
        except Exception as e:
            logger.error("Training failed for %s: %s", sym, e)
            failed = True
    sys.exit(1 if failed else 0)
