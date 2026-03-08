"""
WebSocket server for real-time price streaming.
Clients subscribe to a symbol and receive live quotes every ~5 seconds.
"""
import asyncio
import json
import logging
from typing import Dict, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.market_data.service import market_data_service

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections grouped by stock symbol."""

    def __init__(self):
        # symbol -> set of connected websockets
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, symbol: str):
        await websocket.accept()
        if symbol not in self.active_connections:
            self.active_connections[symbol] = set()
        self.active_connections[symbol].add(websocket)
        logger.info(f"WebSocket connected: {symbol} (total: {len(self.active_connections[symbol])})")

    def disconnect(self, websocket: WebSocket, symbol: str):
        if symbol in self.active_connections:
            self.active_connections[symbol].discard(websocket)
            if not self.active_connections[symbol]:
                del self.active_connections[symbol]
        logger.info(f"WebSocket disconnected: {symbol}")

    async def broadcast(self, symbol: str, message: dict):
        if symbol not in self.active_connections:
            return
        dead_connections = set()
        for ws in self.active_connections[symbol]:
            try:
                await ws.send_json(message)
            except Exception:
                dead_connections.add(ws)
        for ws in dead_connections:
            self.active_connections[symbol].discard(ws)


manager = ConnectionManager()


@router.websocket("/market/{symbol}")
async def websocket_market_stream(websocket: WebSocket, symbol: str):
    """
    WebSocket endpoint for real-time price updates.
    Streams live quote every 5 seconds to all connected clients.
    """
    symbol = symbol.upper()
    await manager.connect(websocket, symbol)
    try:
        # Send initial quote immediately
        quote = await market_data_service.get_quote(symbol)
        await websocket.send_json({"type": "quote", "data": quote})

        # Stream updates every 5 seconds
        while True:
            # Check for incoming messages (e.g., client pings or unsubscribe)
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif data.get("type") == "unsubscribe":
                    break
            except asyncio.TimeoutError:
                pass

            # Fetch and broadcast the latest quote
            try:
                quote = await market_data_service.get_quote(symbol)
                await websocket.send_json({"type": "quote", "data": quote})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {symbol}")
    finally:
        manager.disconnect(websocket, symbol)
