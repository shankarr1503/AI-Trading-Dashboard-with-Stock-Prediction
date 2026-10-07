"""
Bot runner process: `python -m backend.trading.runner`

Runs one agent cycle every BOT_CYCLE_MINUTES. Deploy it as its own service
(docker-compose `bot`) rather than inside the API workers, so exactly one
process drives the bot; the database lease guards against accidental doubles.
"""
import asyncio
import logging
import signal

from backend.config import settings
from backend.trading.agent import TradingAgent

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("trading.runner")


async def main() -> None:
    agent = TradingAgent(owner="runner")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    interval = max(1, settings.BOT_CYCLE_MINUTES) * 60
    logger.info("Trading runner started: mode=%s, universe=%s, every %d min, LLM review=%s",
                settings.TRADING_MODE, ",".join(settings.bot_universe), interval // 60, settings.LLM_REVIEW_ENABLED)
    while not stop.is_set():
        try:
            summary = await agent.run_cycle()
            logger.info("Cycle %s: %s", summary.get("cycle_id"),
                        {k: summary.get(k) for k in ("status", "equity", "entries", "exits", "skipped")})
        except Exception:
            logger.exception("Cycle failed; will retry next interval")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("Trading runner stopped")


if __name__ == "__main__":
    asyncio.run(main())
