"""
Broker adapters.

PaperBroker  — built-in simulator persisted in the database. Fills at the live
               quote adjusted for spread/slippage/fees by the cost model. Default.
AlpacaBroker — Alpaca REST API (paper or live). Entries are GTC bracket orders,
               so a broker-side stop-loss keeps protecting each position on later
               days and if the bot process dies. Fills are confirmed by polling
               the order, never assumed. Live trading needs
               TRADING_MODE=alpaca_live AND ALLOW_LIVE_TRADING=true.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database.models import PaperAccount, PaperPosition
from backend.database.session import utcnow
from backend.trading.costs import cost_model_for
from backend.trading.markets import exchange_for, is_regular_session

logger = logging.getLogger(__name__)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class BrokerPosition:
    symbol: str
    qty: float
    avg_price: float
    market_price: Optional[float] = None   # broker's own mark, used when the quote feed fails


@dataclass
class AccountInfo:
    equity: float
    cash: float


@dataclass
class OrderResult:
    status: str                  # filled | partial | cancelled | rejected | pending
    qty: float = 0.0             # quantity actually filled
    fill_price: Optional[float] = None
    commission: float = 0.0
    broker_order_id: Optional[str] = None
    message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.status in ("filled", "partial") and self.qty > 0


class PaperBroker:
    name = "paper"
    supports_broker_stops = False
    # Splits are applied to the simulated position by the agent (apply_split).
    adjusts_splits = False
    # No exchange calendar here: the agent confirms today is a trading day.
    needs_trading_day_check = True

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
        """Equity at the given prices. Callers must not pass partial price maps (see agent data faults)."""
        acct = await self._account()
        positions = await self.get_positions()
        mv = sum(p.qty * prices.get(p.symbol, p.avg_price) for p in positions)
        return AccountInfo(equity=float(acct.cash) + mv, cash=float(acct.cash))

    async def is_market_open(self, symbol: str) -> bool:
        return is_regular_session(symbol, utcnow())

    async def buy(self, symbol: str, qty: float, ref_price: float, stop: float, target: Optional[float],
                  client_order_id: Optional[str] = None) -> OrderResult:
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
        return OrderResult(status="filled", qty=qty, fill_price=fill, commission=fees, broker_order_id=client_order_id)

    async def sell(self, symbol: str, qty: float, ref_price: float, client_order_id: Optional[str] = None,
                   protective_stop: Optional[float] = None) -> OrderResult:
        if not ref_price or ref_price <= 0:
            return OrderResult(status="rejected", message="No price to fill at")
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
        return OrderResult(status="filled", qty=qty, fill_price=fill, commission=fees, broker_order_id=client_order_id)

    async def apply_split(self, symbol: str, ratio: float) -> None:
        pos = await self.db.get(PaperPosition, symbol)
        if pos is not None and ratio > 0:
            pos.qty = float(pos.qty) * ratio
            pos.avg_price = float(pos.avg_price) / ratio
            await self.db.flush()

    async def update_stop(self, symbol: str, stop: float) -> bool:
        """No resting orders in the simulator; the agent enforces stops each cycle."""
        return True

    async def ensure_protection(self, symbol: str, qty: float, stop: float) -> Dict[str, Any]:
        return {"action": "none", "reason": "paper broker: stops enforced by the agent"}

    async def get_cash_activities(self, since) -> List[Dict[str, Any]]:
        return []

    async def lookup_order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """Paper fills commit atomically with the order journal, so an order still
        'pending' in the journal never executed (its transaction was rolled back)."""
        return None

    async def get_exit_fill(self, symbol: str, entry_order_id: Optional[str], since) -> Optional[Dict[str, Any]]:
        return None


OPEN_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held",
               "pending_replace", "replaced", "calculated", "done_for_day", "pending_cancel"}
# A stop that is being cancelled no longer protects anything.
PROTECTING_STATES = OPEN_STATES - {"pending_cancel"}
DONE_STATES = {"filled", "canceled", "expired", "rejected", "suspended", "stopped"}


class AlpacaBroker:
    supports_broker_stops = True
    # Alpaca rescales positions for splits itself; the agent follows the position.
    adjusts_splits = True

    def __init__(self, api_key: str, api_secret: str, live: bool, transport: Optional[httpx.AsyncBaseTransport] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, fill_timeout: float = 15.0,
                 poll_interval: float = 0.5):
        if not api_key or not api_secret:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET are required for Alpaca trading")
        self.name = "alpaca_live" if live else "alpaca_paper"
        base = "https://api.alpaca.markets" if live else "https://paper-api.alpaca.markets"
        self.client = httpx.AsyncClient(
            base_url=base,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=20,
            transport=transport,
        )
        self._sleep = sleep
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self._clock: Optional[tuple[float, bool]] = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        resp = await self.client.request(method, path, **kw)
        if resp.status_code >= 400:
            logger.warning("Alpaca %s %s → %s %s", method, path, resp.status_code, resp.text[:300])
        return resp

    # ── Account & positions ──

    async def get_positions(self) -> List[BrokerPosition]:
        resp = await self._req("GET", "/v2/positions")
        resp.raise_for_status()
        out = []
        for p in resp.json():
            if float(p["qty"]) > 0:
                mp = p.get("current_price")
                out.append(BrokerPosition(p["symbol"], float(p["qty"]), float(p["avg_entry_price"]),
                                          float(mp) if mp not in (None, "") else None))
        return out

    async def get_account(self, prices: Dict[str, float]) -> AccountInfo:
        resp = await self._req("GET", "/v2/account")
        resp.raise_for_status()
        a = resp.json()
        return AccountInfo(equity=float(a["equity"]), cash=float(a["cash"]))

    async def is_market_open(self, symbol: str) -> bool:
        if exchange_for(symbol) != "US":
            return False
        if self._clock and time.monotonic() - self._clock[0] < 60:
            return self._clock[1]
        resp = await self._req("GET", "/v2/clock")
        resp.raise_for_status()
        is_open = bool(resp.json().get("is_open"))
        self._clock = (time.monotonic(), is_open)
        return is_open

    async def get_cash_activities(self, since) -> List[Dict[str, Any]]:
        """Deposits (+) and withdrawals (−) since `since`, each with its activity id so
        the agent can de-duplicate overlapping windows. Raises if Alpaca can't answer —
        an unknown cash flow must not be mistaken for P&L."""
        params = {"activity_types": "CSD,CSW", "direction": "asc", "page_size": "100"}
        if since:
            params["after"] = since.isoformat()
        resp = await self._req("GET", "/v2/account/activities", params=params)
        resp.raise_for_status()
        out = []
        for act in resp.json():
            amt = abs(float(act.get("net_amount") or 0))
            key = act.get("id") or f"{act.get('activity_type')}:{act.get('date')}:{act.get('net_amount')}"
            out.append({"id": str(key), "amount": amt if act.get("activity_type") == "CSD" else -amt,
                        "date": act.get("date") or act.get("transaction_time")})
        return out

    # ── Orders ──

    async def _get_order(self, order_id: str) -> Optional[dict]:
        resp = await self._req("GET", f"/v2/orders/{order_id}", params={"nested": "true"})
        return resp.json() if resp.status_code < 400 else None

    async def _wait(self, order_id: str, timeout: Optional[float] = None) -> Optional[dict]:
        """Poll until the order reaches a final state (or timeout). Returns the last seen order."""
        deadline = time.monotonic() + (self.fill_timeout if timeout is None else timeout)
        order = await self._get_order(order_id)
        while order is not None and order.get("status") not in DONE_STATES and time.monotonic() < deadline:
            await self._sleep(self.poll_interval)
            order = await self._get_order(order_id)
        return order

    @staticmethod
    def _filled(order: Optional[dict]) -> tuple[float, Optional[float]]:
        if not order:
            return 0.0, None
        qty = float(order.get("filled_qty") or 0)
        px = order.get("filled_avg_price")
        return qty, (float(px) if px not in (None, "") else None)

    async def _open_orders(self, symbol: str) -> list:
        resp = await self._req("GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"})
        resp.raise_for_status()
        return resp.json()

    def _flatten_orders(self, orders: list) -> list:
        flat = []
        for o in orders:
            flat.append(o)
            flat.extend(o.get("legs") or [])
        return flat

    async def buy(self, symbol: str, qty: float, ref_price: float, stop: float, target: Optional[float],
                  client_order_id: Optional[str] = None) -> OrderResult:
        if exchange_for(symbol) != "US":
            return OrderResult(status="rejected", message="Alpaca supports US equities only")
        order = {
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market",
            # GTC so the bracket's stop-loss / take-profit legs survive past today's close.
            "time_in_force": "gtc",
            "order_class": "bracket" if target else "oto",
            "stop_loss": {"stop_price": f"{stop:.2f}"},
        }
        if target:
            order["take_profit"] = {"limit_price": f"{target:.2f}"}
        if client_order_id:
            order["client_order_id"] = client_order_id
        resp = await self._req("POST", "/v2/orders", json=order)
        if resp.status_code >= 400:
            return OrderResult(status="rejected", message=resp.text[:300])
        oid = resp.json().get("id")
        final = await self._wait(oid)
        filled_qty, px = self._filled(final)
        status = (final or {}).get("status")
        if status == "filled":
            return OrderResult(status="filled", qty=filled_qty, fill_price=px, broker_order_id=oid)
        # Not (fully) filled in time: cancel the remainder rather than leave an unknown order working.
        await self._req("DELETE", f"/v2/orders/{oid}")
        final = await self._wait(oid, timeout=5)
        if final is None or final.get("status") not in DONE_STATES:
            # The cancel is not confirmed (or the order can't be read): it may still fill.
            # 'pending' keeps it in the journal; the next cycle resolves it by client_order_id.
            return OrderResult(status="pending", broker_order_id=oid,
                               message=f"Entry outcome unknown (status {(final or {}).get('status')}); will reconcile")
        filled_qty, px = self._filled(final)
        if filled_qty > 0:
            return OrderResult(status="partial", qty=filled_qty, fill_price=px, broker_order_id=oid,
                               message=f"Partially filled {filled_qty}/{int(qty)}; remainder cancelled")
        return OrderResult(status="cancelled", broker_order_id=oid,
                           message=f"Entry not filled within {self.fill_timeout:.0f}s (status {status}); cancelled")

    async def _cancel_all(self, symbol: str) -> list:
        """Cancel every working order on `symbol` (parents and bracket legs) and wait for confirmation."""
        working = [o for o in self._flatten_orders(await self._open_orders(symbol)) if o.get("status") in OPEN_STATES]
        for o in working:
            await self._req("DELETE", f"/v2/orders/{o['id']}")   # 422 = already done: fine
        results = []
        for o in working:
            results.append(await self._wait(o["id"], timeout=10))
        return results

    async def _position_qty(self, symbol: str) -> float:
        resp = await self._req("GET", f"/v2/positions/{symbol}")
        if resp.status_code == 404:
            return 0.0
        resp.raise_for_status()
        return float(resp.json().get("qty") or 0)

    async def sell(self, symbol: str, qty: float, ref_price: float, client_order_id: Optional[str] = None,
                   protective_stop: Optional[float] = None) -> OrderResult:
        await self._cancel_all(symbol)
        held = await self._position_qty(symbol)
        if held <= 0:
            # A stop/target leg filled while we were cancelling: the position is already closed.
            fill = await self.get_recent_fill(symbol, "sell", since=utcnow() - timedelta(hours=1))
            return OrderResult(status="filled", qty=qty, fill_price=(fill or {}).get("price"),
                               message="Position already closed by a resting order", extra={"via": "bracket_leg"})
        sell_qty = min(qty, held)
        body = {"symbol": symbol, "qty": str(sell_qty), "side": "sell", "type": "market", "time_in_force": "day"}
        if client_order_id:
            body["client_order_id"] = client_order_id
        resp = await self._req("POST", "/v2/orders", json=body)
        final = await self._wait(resp.json().get("id")) if resp.status_code < 400 else None
        filled_qty, px = self._filled(final)
        if final and final.get("status") == "filled":
            return OrderResult(status="filled", qty=filled_qty, fill_price=px, broker_order_id=final.get("id"))

        # Exit failed: never leave the position naked — put a GTC stop back.
        message = resp.text[:200] if resp.status_code >= 400 else f"Exit not filled (status {(final or {}).get('status')})"
        if final and final.get("id") and final.get("status") not in DONE_STATES:
            await self._req("DELETE", f"/v2/orders/{final['id']}")
            after_cancel = await self._wait(final["id"], timeout=5)
            if after_cancel is not None:
                final = after_cancel
                filled_qty, px = self._filled(final)
                if final.get("status") == "filled":    # the fill won the race against our cancel
                    return OrderResult(status="filled", qty=filled_qty, fill_price=px, broker_order_id=final.get("id"))
        remaining = await self._position_qty(symbol)
        reprotect = None
        if remaining > 0 and protective_stop:
            reprotect = await self.ensure_protection(symbol, remaining, protective_stop)
        status = "partial" if filled_qty > 0 else "rejected"
        return OrderResult(status=status, qty=filled_qty, fill_price=px, broker_order_id=(final or {}).get("id"),
                           message=message, extra={"reprotected": reprotect})

    async def ensure_protection(self, symbol: str, qty: float, stop: float) -> Dict[str, Any]:
        """Make sure resting GTC sell-stop orders cover the whole position."""
        stops = [o for o in self._flatten_orders(await self._open_orders(symbol))
                 if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit")
                 and o.get("status") in PROTECTING_STATES]
        covered = sum(float(o.get("qty") or 0) for o in stops)
        missing = int(qty - covered)
        if missing <= 0:
            return {"action": "ok", "covered": covered}
        resp = await self._req("POST", "/v2/orders", json={
            "symbol": symbol, "qty": str(missing), "side": "sell", "type": "stop",
            "stop_price": f"{stop:.2f}", "time_in_force": "gtc",
        })
        if resp.status_code >= 400:
            return {"action": "failed", "missing": missing, "error": resp.text[:200]}
        return {"action": "placed", "qty": missing, "stop": round(stop, 2), "order_id": resp.json().get("id")}

    async def update_stop(self, symbol: str, stop: float) -> bool:
        """Raise broker-side stops to the bot's trailing stop. Returns False if any update failed."""
        ok = True
        for leg in self._flatten_orders(await self._open_orders(symbol)):
            if leg.get("side") == "sell" and leg.get("type") in ("stop", "stop_limit") \
                    and leg.get("status") in PROTECTING_STATES:
                if stop > float(leg.get("stop_price") or 0) + 0.005:
                    resp = await self._req("PATCH", f"/v2/orders/{leg['id']}", json={"stop_price": f"{stop:.2f}"})
                    ok = ok and resp.status_code < 400
        return ok

    async def get_recent_fill(self, symbol: str, side: str, since) -> Optional[Dict[str, Any]]:
        """Most recent filled `side` order (or bracket leg) on `symbol` that FILLED after `since`.
        Alpaca's `after` filter is on submission time and bracket legs are nested under a
        parent submitted before the position opened, so the query window starts earlier
        and the fill time is checked here."""
        params = {"status": "closed", "symbols": symbol, "nested": "true", "direction": "desc", "limit": "50"}
        if since:
            params["after"] = (since - timedelta(days=7)).isoformat()
        resp = await self._req("GET", "/v2/orders", params=params)
        if resp.status_code >= 400:
            return None
        fills = []
        for o in self._flatten_orders(resp.json()):
            if o.get("side") != side or float(o.get("filled_qty") or 0) <= 0 or not o.get("filled_avg_price"):
                continue
            filled_at = _parse_ts(o.get("filled_at"))
            if since and filled_at is not None and filled_at < since:
                continue
            fills.append((filled_at or since, o))
        if not fills:
            return None
        _, o = max(fills, key=lambda t: t[0] or datetime.min.replace(tzinfo=timezone.utc))
        return {"price": float(o["filled_avg_price"]), "qty": float(o.get("filled_qty") or 0),
                "filled_at": o.get("filled_at"), "order_id": o.get("id")}

    async def get_exit_fill(self, symbol: str, entry_order_id: Optional[str], since) -> Optional[Dict[str, Any]]:
        """Where a position closed outside the bot: the filled bracket legs of its entry
        order (quantity-weighted), else the latest sell fill after `since`."""
        if entry_order_id:
            parent = await self._get_order(entry_order_id)
            legs = [leg for leg in (parent or {}).get("legs") or []
                    if leg.get("side") == "sell" and float(leg.get("filled_qty") or 0) > 0 and leg.get("filled_avg_price")]
            if legs:
                qty = sum(float(leg["filled_qty"]) for leg in legs)
                px = sum(float(leg["filled_qty"]) * float(leg["filled_avg_price"]) for leg in legs) / qty
                return {"price": px, "qty": qty, "filled_at": max(str(leg.get("filled_at") or "") for leg in legs),
                        "order_id": legs[0].get("id"), "via": "bracket_leg"}
        return await self.get_recent_fill(symbol, "sell", since)

    async def lookup_order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """Resolve an order whose outcome the journal doesn't know. Cancels it if it is
        still working (the bot never leaves entries resting). None = never reached Alpaca."""
        resp = await self._req("GET", "/v2/orders:by_client_order_id",
                               params={"client_order_id": client_order_id, "nested": "true"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        order = resp.json()
        if order.get("status") not in DONE_STATES:
            await self._req("DELETE", f"/v2/orders/{order['id']}")
            order = await self._wait(order["id"], timeout=5) or order
        filled_qty, px = self._filled(order)
        return {"status": order.get("status"), "done": order.get("status") in DONE_STATES,
                "filled_qty": filled_qty, "fill_price": px, "order_id": order.get("id")}

    async def apply_split(self, symbol: str, ratio: float) -> None:
        """Alpaca adjusts quantities and prices for splits itself."""
        return None


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
