"""
Bot runner process: `python -m backend.trading.runner`

Runs one agent cycle every BOT_CYCLE_MINUTES. Deploy it as its own service
(docker-compose `bot`) rather than inside the API workers, so exactly one
process drives the bot; the database lease guards against accidental doubles.

The loop itself (`run_forever`) is shared with the desktop sidecar
(backend/desktop.py), which runs it as a background task next to the API.
"""
import asyncio
import logging
import signal
from typing import Optional

from backend.config import settings
from backend.trading.agent import TradingAgent

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("trading.runner")


async def run_forever(stop: asyncio.Event, agent: Optional[TradingAgent] = None,
                      interval: Optional[float] = None) -> None:
    """
    Run a cycle, then wait BOT_CYCLE_MINUTES (or until `stop` is set), until `stop`
    is set. `stop` is only checked between cycles: a cycle that has started always
    runs to completion, so stopping can never interrupt an order mid-flight.
    `interval` (seconds) overrides BOT_CYCLE_MINUTES; tests use it.
    """
    agent = agent or TradingAgent(owner="runner")
    if interval is None:
        interval = max(1, settings.BOT_CYCLE_MINUTES) * 60
    if settings.LLM_REVIEW_ENABLED and not settings.ANTHROPIC_API_KEY:
        logger.warning("LLM_REVIEW_ENABLED=true but ANTHROPIC_API_KEY is empty: reviews will use the fail mode (%s)",
                       settings.LLM_REVIEW_FAIL_MODE)
    if not settings.ALERT_WEBHOOK_URL:
        logger.warning("ALERT_WEBHOOK_URL is not set: kill-switch and failure alerts only go to the log")
    logger.info("Trading runner started: mode=%s, universe=%s, every %d min, LLM review=%s",
                settings.TRADING_MODE, ",".join(settings.bot_universe), interval // 60, settings.LLM_REVIEW_ENABLED)
    while not stop.is_set():
        # A cycle always finishes (it never raises); SIGTERM only stops the loop
        # between cycles, so a deploy cannot interrupt an order mid-flight.
        try:
            summary = await agent.run_cycle()
            logger.info("Cycle %s: %s", summary.get("cycle_id"),
                        {k: summary.get(k) for k in ("status", "equity", "entries", "exits", "skipped", "error")})
        except Exception:
            logger.exception("Cycle failed unexpectedly; will retry next interval")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("Trading runner stopped")


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await run_forever(stop, TradingAgent(owner="runner"))


if __name__ == "__main__":
    asyncio.run(main())
