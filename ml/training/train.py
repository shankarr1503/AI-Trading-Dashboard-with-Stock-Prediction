"""
Training Script — train all ML models for a given symbol and save to disk.
Usage:
    python -m ml.training.train --symbol AAPL --period 2y
"""
import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def train_symbol(symbol: str, period: str = "2y", model_dir: str = "ml/saved_models"):
    logger.info(f"Starting training for {symbol} (period={period})")
    try:
        from ml.ensemble.predictor import EnsemblePredictor
        predictor = EnsemblePredictor(model_dir=model_dir)
        predictor.train_all(symbol.upper())
        logger.info(f"✅ Training complete for {symbol}. Models saved to {model_dir}/{symbol}/")
    except Exception as e:
        logger.error(f"Training failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train ML models for a stock symbol")
    parser.add_argument("--symbol", type=str, default="AAPL", help="Stock symbol (e.g. AAPL)")
    parser.add_argument("--period", type=str, default="2y", help="Historical data period (e.g. 1y, 2y)")
    parser.add_argument("--model-dir", type=str, default="ml/saved_models", help="Directory to save models")
    args = parser.parse_args()

    train_symbol(args.symbol, args.period, args.model_dir)
