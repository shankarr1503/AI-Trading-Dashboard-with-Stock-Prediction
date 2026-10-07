"""Container healthcheck for the bot runner: exit 1 if cycles are stale or failing."""
import asyncio
import sys

from backend.config import settings
from backend.database.session import AsyncSessionLocal, as_utc, utcnow
from backend.trading.agent import get_state


async def main() -> int:
    async with AsyncSessionLocal() as db:
        state = await get_state(db)
    if state.last_cycle_at is None:
        return 0  # just started
    age = (utcnow() - as_utc(state.last_cycle_at)).total_seconds()
    stale = age > 3 * max(1, settings.BOT_CYCLE_MINUTES) * 60
    failing = (state.consecutive_failures or 0) >= 3
    if stale or failing:
        print(f"unhealthy: last cycle {age:.0f}s ago, consecutive failures {state.consecutive_failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
