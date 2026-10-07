"""AlpacaBroker against an in-memory Alpaca API simulator (httpx.MockTransport)."""
import json
import uuid

import httpx
import pytest

from backend.trading.broker import AlpacaBroker


class FakeAlpaca:
    """Minimal Alpaca v2 semantics: async fills, bracket legs nested under the parent,
    asynchronous cancels (pending_cancel → canceled), positions, activities."""

    def __init__(self, fill_after_polls=1, fill_price=100.5, reject_market_sells=False, never_fill=False):
        self.orders = {}
        self.positions = {}
        self.fill_after = fill_after_polls
        self.fill_price = fill_price
        self.reject_market_sells = reject_market_sells
        self.never_fill = never_fill
        self.polls = {}
        self.requests = []
        self.activities = []

    def _order(self, **kw):
        o = {"id": uuid.uuid4().hex, "status": "new", "filled_qty": "0", "filled_avg_price": None, "legs": None, **kw}
        self.orders[o["id"]] = o
        return o

    def _tick(self, o):
        """Advance an order's state when it is polled."""
        if o["status"] == "pending_cancel":
            o["status"] = "canceled"
            return
        if o["status"] in ("new", "accepted") and o.get("type") == "market" and not self.never_fill:
            self.polls[o["id"]] = self.polls.get(o["id"], 0) + 1
            if self.polls[o["id"]] >= self.fill_after:
                o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(self.fill_price))
                sym, q = o["symbol"], float(o["qty"])
                cur = self.positions.get(sym, {"qty": 0.0, "avg": 0.0})
                if o["side"] == "buy":
                    cur = {"qty": cur["qty"] + q, "avg": self.fill_price}
                    for leg in o.get("legs") or []:
                        leg["status"] = "new"
                else:
                    cur = {"qty": cur["qty"] - q, "avg": cur["avg"]}
                if cur["qty"] <= 0:
                    self.positions.pop(sym, None)
                else:
                    self.positions[sym] = cur

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.content))
        path, m = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/v2/orders" and m == "POST":
            if body.get("type") == "market" and body["side"] == "sell" and self.reject_market_sells:
                return httpx.Response(403, json={"message": "insufficient qty available"})
            legs = None
            if body.get("order_class") in ("bracket", "oto"):
                legs = [self._order(symbol=body["symbol"], side="sell", type="stop", qty=body["qty"],
                                    stop_price=body["stop_loss"]["stop_price"], status="held",
                                    time_in_force=body["time_in_force"])]
                if body.get("take_profit"):
                    legs.append(self._order(symbol=body["symbol"], side="sell", type="limit", qty=body["qty"],
                                            limit_price=body["take_profit"]["limit_price"], status="held",
                                            time_in_force=body["time_in_force"]))
            o = self._order(**{k: v for k, v in body.items() if k not in ("stop_loss", "take_profit")}, legs=legs)
            return httpx.Response(200, json=o)
        if path == "/v2/orders" and m == "GET":
            sym = request.url.params.get("symbols")
            status = request.url.params.get("status")
            leg_ids = {leg["id"] for o in self.orders.values() for leg in (o.get("legs") or [])}
            out = []
            for o in self.orders.values():
                if o["id"] in leg_ids or o["symbol"] != sym:
                    continue
                live = o["status"] not in ("filled", "canceled", "expired", "rejected") or \
                    any(leg["status"] not in ("canceled", "filled") for leg in o.get("legs") or [])
                if (status == "open" and live) or (status == "closed" and not live):
                    out.append(o)
            return httpx.Response(200, json=out)
        if path.startswith("/v2/orders/") and m == "GET":
            o = self.orders.get(path.rsplit("/", 1)[1])
            if not o:
                return httpx.Response(404, json={})
            self._tick(o)
            return httpx.Response(200, json=o)
        if path.startswith("/v2/orders/") and m == "DELETE":
            o = self.orders.get(path.rsplit("/", 1)[1])
            if not o or o["status"] in ("filled", "canceled"):
                return httpx.Response(422, json={"message": "order is not cancelable"})
            o["status"] = "pending_cancel"
            return httpx.Response(204)
        if path.startswith("/v2/orders/") and m == "PATCH":
            o = self.orders[path.rsplit("/", 1)[1]]
            o["stop_price"] = body["stop_price"]
            return httpx.Response(200, json=o)
        if path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            p = self.positions.get(sym)
            if not p:
                return httpx.Response(404, json={"message": "position does not exist"})
            return httpx.Response(200, json={"symbol": sym, "qty": str(p["qty"]), "avg_entry_price": str(p["avg"])})
        if path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(p["avg"]),
                                              "current_price": str(self.fill_price)} for s, p in self.positions.items()])
        if path == "/v2/account/activities":
            return httpx.Response(200, json=self.activities)
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": True})
        return httpx.Response(404, json={"path": path})


async def no_sleep(_):
    return None


def broker(fake: FakeAlpaca) -> AlpacaBroker:
    return AlpacaBroker("k", "s", live=False, transport=httpx.MockTransport(fake.handler), sleep=no_sleep,
                        fill_timeout=0.2, poll_interval=0)


async def test_entry_is_gtc_bracket_and_waits_for_real_fill():
    fake = FakeAlpaca(fill_after_polls=3, fill_price=101.25)
    b = broker(fake)
    res = await b.buy("AAPL", 10, ref_price=100.0, stop=95.0, target=110.0, client_order_id="c1")
    assert res.status == "filled" and res.qty == 10 and res.fill_price == 101.25   # actual fill, not the quote
    sent = json.loads([c for m, p, c in fake.requests if m == "POST"][0])
    assert sent["time_in_force"] == "gtc" and sent["order_class"] == "bracket" and sent["client_order_id"] == "c1"
    legs = next(o for o in fake.orders.values() if o.get("legs"))["legs"]
    assert {leg["type"] for leg in legs} == {"stop", "limit"} and all(leg["time_in_force"] == "gtc" for leg in legs)


async def test_unfilled_entry_is_cancelled_not_recorded_as_filled():
    fake = FakeAlpaca(never_fill=True)
    res = await broker(fake).buy("AAPL", 10, 100.0, 95.0, 110.0)
    assert res.status == "cancelled" and not res.filled
    assert any(m == "DELETE" for m, _, _ in fake.requests)


async def test_exit_cancels_every_leg_before_selling():
    fake = FakeAlpaca()
    b = broker(fake)
    await b.buy("AAPL", 10, 100.0, 95.0, 110.0)
    res = await b.sell("AAPL", 10, 100.0, protective_stop=95.0)
    assert res.status == "filled" and res.qty == 10
    legs = [leg for o in fake.orders.values() for leg in (o.get("legs") or [])]
    assert legs and all(leg["status"] == "canceled" for leg in legs)
    assert "AAPL" not in fake.positions


async def test_failed_exit_re_arms_a_protective_stop():
    fake = FakeAlpaca(reject_market_sells=True)
    b = broker(fake)
    await b.buy("AAPL", 10, 100.0, 95.0, 110.0)
    res = await b.sell("AAPL", 10, 100.0, protective_stop=95.0)
    assert res.status == "rejected"
    assert res.extra["reprotected"]["action"] == "placed"
    stops = [o for o in fake.orders.values() if o.get("type") == "stop" and o["status"] in ("new", "accepted", "held")]
    assert any(o.get("time_in_force") == "gtc" and float(o["qty"]) == 10 for o in stops)


async def test_ensure_protection_and_trailing_update():
    fake = FakeAlpaca()
    b = broker(fake)
    await b.buy("AAPL", 10, 100.0, 95.0, 110.0)
    assert (await b.ensure_protection("AAPL", 10, 95.0))["action"] == "ok"
    assert await b.update_stop("AAPL", 97.5)
    stop_leg = next(leg for o in fake.orders.values() for leg in (o.get("legs") or []) if leg["type"] == "stop")
    assert stop_leg["stop_price"] == "97.50"
    for leg in next(o for o in fake.orders.values() if o.get("legs"))["legs"]:
        leg["status"] = "expired"                         # e.g. legs gone at the broker
    placed = await b.ensure_protection("AAPL", 10, 97.5)
    assert placed["action"] == "placed" and placed["qty"] == 10


async def test_cash_flows_signed():
    fake = FakeAlpaca()
    fake.activities = [{"activity_type": "CSD", "net_amount": "5000"}, {"activity_type": "CSW", "net_amount": "-12000"}]
    from datetime import datetime, timezone

    assert await broker(fake).get_net_cash_flows(datetime.now(timezone.utc)) == pytest.approx(-7000)
