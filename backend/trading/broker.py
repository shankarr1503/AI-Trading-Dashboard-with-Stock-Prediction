"""
Broker adapters.

PaperBroker  — built-in simulator persisted in the database. Fills at the live
               quote adjusted for spread/slippage/fees by the cost model. Default.
AlpacaBroker — Alpaca REST API (paper or live). Entries are bracket orders, so a
               broker-side stop-loss protects every position even if the bot
               process dies. Live trading needs TRADING_MODE=alpaca_live AND
               ALLOW_LIVE_TRADING=true.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database.models import PaperAccount, PaperPosition
from backend.database.session import utcnow
from backend.trading.costs import cost_model_for
from backend.trading.markets import exchange_for, is_regular_session

logger = logging.getLogger(__name__)


@dataclass
class BrokerPosition:
    symbol: str
    qty: float
    avg_price: float


@dataclass
class AccountInfo:
    equity: float
    cash: float


@dataclass
class OrderResult:
    status: str                  # filled | submitted | rejected
    qty: float = 0.0
    fill_price: Optional[float] = None
    commission: float = 0.0
    broker_order_id: Optional[str] = None
    message: str = ""


class PaperBroker:
    name = "paper"

    def __init__(self, db: AsyncSession, initial_capital: float):
        self.db = db
        self.initial_capital = initial_capital

    async def _account(self) -> PaperAccount:
        acct = await self.db.get(PaperAccount, 1)
        if acct is None:
            acct = PaperAccount(id=1, cash=self.initial_capital, initial_capital=self.initial_capital)
            self.db.add(acct)
            await self.db.flush()
        return acct

    async def get_positions(self) -> List[BrokerPosition]:
        rows = (await self.db.execute(select(PaperPosition))).scalars().all()
        return [BrokerPosition(r.symbol, float(r.qty), float(r.avg_price)) for r in rows if r.qty > 0]

    async def get_account(self, prices: Dict[str, float]) -> AccountInfo:
        acct = await self._account()
        positions = await self.get_positions()
        mv = sum(p.qty * prices.get(p.symbol, p.avg_price) for p in positions)
        return AccountInfo(equity=float(acct.cash) + mv, cash=float(acct.cash))

    async def is_market_open(self, symbol: str) -> bool:
        return is_regular_session(symbol, utcnow())

    async def buy(self, symbol: str, qty: float, ref_price: float, stop: float, target: Optional[float]) -> OrderResult:
        cm = cost_model_for(symbol)
        fill = cm.fill_price("BUY", ref_price)
        fees = cm.fees("BUY", qty, fill)
        acct = await self._account()
        total = qty * fill + fees
        if total > float(acct.cash) + 1e-6:
            return OrderResult(status="rejected", message="Insufficient paper cash")
        acct.cash = float(acct.cash) - total
        pos = await self.db.get(PaperPosition, symbol)
        if pos is None:
            self.db.add(PaperPosition(symbol=symbol, qty=qty, avg_price=fill))
        else:
            new_qty = float(pos.qty) + qty
            pos.avg_price = (float(pos.avg_price) * float(pos.qty) + fill * qty) / new_qty
            pos.qty = new_qty
        await self.db.flush()
        return OrderResult(status="filled", qty=qty, fill_price=fill, commission=fees)

    async def sell(self, symbol: str, qty: float, ref_price: float) -> OrderResult:
        pos = await self.db.get(PaperPosition, symbol)
        if pos is None or float(pos.qty) <= 0:
            return OrderResult(status="rejected", message="No paper position")
        qty = min(qty, float(pos.qty))
        cm = cost_model_for(symbol)
        fill = cm.fill_price("SELL", ref_price)
        fees = cm.fees("SELL", qty, fill)
        acct = await self._account()
        acct.cash = float(acct.cash) + qty * fill - fees
        remaining = float(pos.qty) - qty
        if remaining <= 1e-9:
            await self.db.delete(pos)
        else:
            pos.qty = remaining
        await self.db.flush()
        return OrderResult(status="filled", qty=qty, fill_price=fill, commission=fees)

    async def update_stop(self, symbol: str, stop: float) -> None:
        """The paper broker has no resting orders; the agent enforces stops each cycle."""
        return None


class AlpacaBroker:
    def __init__(self, api_key: str, api_secret: str, live: bool):
        if not api_key or not api_secret:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET are required for Alpaca trading")
        self.name = "alpaca_live" if live else "alpaca_paper"
        base = "https://api.alpaca.markets" if live else "https://paper-api.alpaca.markets"
        self.client = httpx.AsyncClient(
            base_url=base,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=20,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        resp = await self.client.request(method, path, **kw)
        if resp.status_code >= 400:
            logger.warning("Alpaca %s %s → %s %s", method, path, resp.status_code, resp.text[:300])
        return resp

    async def get_positions(self) -> List[BrokerPosition]:
        resp = await self._req("GET", "/v2/positions")
        resp.raise_for_status()
        return [
            BrokerPosition(p["symbol"], float(p["qty"]), float(p["avg_entry_price"]))
            for p in resp.json() if float(p["qty"]) > 0
        ]

    async def get_account(self, prices: Dict[str, float]) -> AccountInfo:
        resp = await self._req("GET", "/v2/account")
        resp.raise_for_status()
        a = resp.json()
        return AccountInfo(equity=float(a["equity"]), cash=float(a["cash"]))

    async def is_market_open(self, symbol: str) -> bool:
        if exchange_for(symbol) != "US":
            return False
        resp = await self._req("GET", "/v2/clock")
        resp.raise_for_status()
        return bool(resp.json().get("is_open"))

    async def buy(self, symbol: str, qty: float, ref_price: float, stop: float, target: Optional[float]) -> OrderResult:
        if exchange_for(symbol) != "US":
            return OrderResult(status="rejected", message="Alpaca supports US equities only")
        order = {
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "stop_loss": {"stop_price": f"{stop:.2f}"},
        }
        if target:
            order["take_profit"] = {"limit_price": f"{target:.2f}"}
        else:
            order["order_class"] = "oto"
        resp = await self._req("POST", "/v2/orders", json=order)
        if resp.status_code >= 400:
            return OrderResult(status="rejected", message=resp.text[:300])
        data = resp.json()
        fill = float(data["filled_avg_price"]) if data.get("filled_avg_price") else None
        return OrderResult(status="filled" if fill else "submitted", qty=qty, fill_price=fill or ref_price,
                           broker_order_id=data.get("id"))

    async def _open_orders(self, symbol: str) -> list:
        resp = await self._req("GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"})
        resp.raise_for_status()
        return resp.json()

    async def sell(self, symbol: str, qty: float, ref_price: float) -> OrderResult:
        # Cancel the bracket's resting legs first, otherwise the shares are locked.
        for o in await self._open_orders(symbol):
            await self._req("DELETE", f"/v2/orders/{o['id']}")
        resp = await self._req("DELETE", f"/v2/positions/{symbol}")
        if resp.status_code >= 400:
            return OrderResult(status="rejected", message=resp.text[:300])
        data = resp.json()
        fill = float(data["filled_avg_price"]) if data.get("filled_avg_price") else None
        return OrderResult(status="filled" if fill else "submitted", qty=qty, fill_price=fill or ref_price,
                           broker_order_id=data.get("id"))

    async def update_stop(self, symbol: str, stop: float) -> None:
        """Raise the broker-side stop leg to the bot's trailing stop."""
        for o in await self._open_orders(symbol):
            legs = [o] + (o.get("legs") or [])
            for leg in legs:
                if leg.get("side") == "sell" and leg.get("type") in ("stop", "stop_limit"):
                    current = float(leg.get("stop_price") or 0)
                    if stop > current:
                        await self._req("PATCH", f"/v2/orders/{leg['id']}", json={"stop_price": f"{stop:.2f}"})


def make_broker(db: AsyncSession):
    mode = settings.TRADING_MODE
    if mode == "paper":
        return PaperBroker(db, settings.BOT_INITIAL_CAPITAL)
    if mode == "alpaca_paper":
        return AlpacaBroker(settings.ALPACA_API_KEY, settings.ALPACA_API_SECRET, live=False)
    if mode == "alpaca_live":
        if not settings.ALLOW_LIVE_TRADING:
            raise RuntimeError("Live trading is disabled (ALLOW_LIVE_TRADING=false)")
        return AlpacaBroker(settings.ALPACA_API_KEY, settings.ALPACA_API_SECRET, live=True)
    raise RuntimeError(f"Unknown TRADING_MODE {mode}")
