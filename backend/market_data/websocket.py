"""
WebSocket server for real-time price streaming.

One background poller runs per subscribed symbol and broadcasts each quote to
every client watching that symbol, so N viewers cost one upstream fetch, not N.
"""
import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.auth.utils import decode_token
from backend.market_data.service import market_data_service, validate_symbol
from backend.ratelimit import client_ip

logger = logging.getLogger(__name__)
router = APIRouter()

POLL_SECONDS = 5.0
MAX_SYMBOLS = 50          # distinct symbols with a live poller
MAX_CONNECTIONS_PER_IP = 5


class ConnectionManager:
    """Tracks subscribers per symbol and owns one polling task per symbol."""

    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._pollers: Dict[str, asyncio.Task] = {}
        self.per_ip: Dict[str, int] = {}

    async def connect(self, websocket: WebSocket, symbol: str):
        await websocket.accept()
        self.active_connections.setdefault(symbol, set()).add(websocket)
        if symbol not in self._pollers or self._pollers[symbol].done():
            self._pollers[symbol] = asyncio.create_task(self._poll(symbol))
        logger.info("WebSocket connected: %s (subscribers: %d)", symbol, len(self.active_connections[symbol]))

    def disconnect(self, websocket: WebSocket, symbol: str):
        subs = self.active_connections.get(symbol)
        if subs is not None:
            subs.discard(websocket)
            if not subs:
                del self.active_connections[symbol]
                task = self._pollers.pop(symbol, None)
                if task:
                    task.cancel()

    async def broadcast(self, symbol: str, message: dict):
        dead = []
        for ws in list(self.active_connections.get(symbol, ())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, symbol)

    async def _poll(self, symbol: str):
        while symbol in self.active_connections:
            try:
                quote = await market_data_service.get_quote(symbol)
                await self.broadcast(symbol, {"type": "quote", "data": quote})
            except ValueError as e:
                await self.broadcast(symbol, {"type": "error", "message": str(e)})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Quote poller failed for %s", symbol)
            await asyncio.sleep(POLL_SECONDS)


manager = ConnectionManager()


@router.websocket("/market/{symbol}")
async def websocket_market_stream(websocket: WebSocket, symbol: str):
    """
    Stream live quotes for `symbol` every ~5 seconds; requires `?token=<access token>`.
    Send {"type":"ping"} for a pong. Connections per IP and polled symbols are capped
    so the shared market-data quota (which the bot depends on) can't be exhausted.
    """
    payload = decode_token(websocket.query_params.get("token", ""))
    if not payload or payload.get("type") != "access":
        await websocket.close(code=1008)
        return
    try:
        symbol = validate_symbol(symbol)
    except ValueError:
        await websocket.close(code=1008)
        return
    ip = client_ip(websocket)  # type: ignore[arg-type]
    if manager.per_ip.get(ip, 0) >= MAX_CONNECTIONS_PER_IP or \
            (symbol not in manager.active_connections and len(manager.active_connections) >= MAX_SYMBOLS):
        await websocket.close(code=1013)
        return
    try:
        await market_data_service.get_quote(symbol)   # unknown symbols never get a poller
    except ValueError:
        await websocket.close(code=1008)
        return
    manager.per_ip[ip] = manager.per_ip.get(ip, 0) + 1
    await manager.connect(websocket, symbol)
    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif data.get("type") == "unsubscribe":
                break
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, symbol)
        manager.per_ip[ip] = max(0, manager.per_ip.get(ip, 1) - 1)
