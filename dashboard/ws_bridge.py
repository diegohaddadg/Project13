"""WebSocket bridge — broadcasts bot state to connected dashboard clients.

Read-only: never mutates trading state. Handles client lifecycle robustly.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from dashboard.state_adapter import StateAdapter
from utils.logger import get_logger
import config

log = get_logger("ws_bridge")

_DEBUG_LOG = "/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log"


class WebSocketBridge:
    """Broadcasts state snapshots to connected WebSocket clients."""

    def __init__(self, adapter: StateAdapter):
        self._adapter = adapter
        self._clients: Set[WebSocket] = set()
        self._running = False

    async def connect(self, ws: WebSocket) -> None:
        """Accept a new client connection."""
        await ws.accept()
        self._clients.add(ws)
        log.info(f"Dashboard client connected ({len(self._clients)} total)")

    async def disconnect(self, ws: WebSocket) -> None:
        """Remove a disconnected client."""
        self._clients.discard(ws)
        log.info(f"Dashboard client disconnected ({len(self._clients)} total)")

    async def handle_client(self, ws: WebSocket) -> None:
        """Keep a client connection alive, receiving pings/messages."""
        try:
            while True:
                # Read messages to detect disconnects; ignore content
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await self.disconnect(ws)

    async def start_broadcasting(self) -> None:
        """Broadcast state to all connected clients at configured interval."""
        self._running = True
        interval = config.DASHBOARD_WS_UPDATE_INTERVAL_MS / 1000.0
        log.info(f"WebSocket bridge broadcasting every {config.DASHBOARD_WS_UPDATE_INTERVAL_MS}ms")

        while self._running:
            if self._clients:
                try:
                    snapshot = self._adapter.get_full_snapshot()
                    # region agent log
                    try:
                        with open(_DEBUG_LOG, "a") as _df:
                            _df.write(
                                json.dumps(
                                    {
                                        "sessionId": "16560d",
                                        "hypothesisId": "WS1",
                                        "location": "ws_bridge.py:start_broadcasting",
                                        "message": "snapshot built, attempting JSON",
                                        "data": {"ts": snapshot.get("ts")},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    payload = json.dumps(snapshot, default=str, allow_nan=False)
                    # Broadcast to all clients, remove dead ones
                    dead = set()
                    for client in self._clients.copy():
                        try:
                            await client.send_text(payload)
                        except Exception:
                            dead.add(client)
                    for d in dead:
                        self._clients.discard(d)
                except Exception as e:
                    tb = traceback.format_exc()
                    log.error(f"WebSocket broadcast error: {e}\n{tb}")
                    # region agent log
                    try:
                        with open(_DEBUG_LOG, "a") as _df:
                            _df.write(
                                json.dumps(
                                    {
                                        "sessionId": "16560d",
                                        "hypothesisId": "WS1",
                                        "location": "ws_bridge.py:start_broadcasting",
                                        "message": "broadcast or JSON failed",
                                        "data": {"error": str(e), "traceback": tb},
                                        "timestamp": int(time.time() * 1000),
                                    }
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion

            await asyncio.sleep(interval)

    async def stop(self) -> None:
        """Stop broadcasting and close all connections."""
        self._running = False
        for client in self._clients.copy():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        log.info("WebSocket bridge stopped")
