"""FastAPI dashboard server — read-only monitoring interface.

Serves static frontend files and exposes REST + WebSocket endpoints.
The only write action allowed is kill switch activation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, Request, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from dashboard.state_adapter import StateAdapter
from dashboard.ws_bridge import WebSocketBridge
from risk.kill_switch import KillSwitch
from utils.logger import get_logger
import config

log = get_logger("dashboard")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    adapter: StateAdapter,
    kill_switch: KillSwitch,
    ws_bridge: WebSocketBridge,
) -> FastAPI:
    """Create the FastAPI dashboard application."""

    app = FastAPI(title="Project13 Dashboard", docs_url=None, redoc_url=None)

    # --- Auth middleware ---

    def _check_auth(request: Request) -> None:
        """Check auth token if configured."""
        token = config.DASHBOARD_AUTH_TOKEN
        if not token:
            return  # No auth required
        # Accept via header or query param
        req_token = (
            request.headers.get("Authorization", "").replace("Bearer ", "")
            or request.query_params.get("token", "")
        )
        if req_token != token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # --- REST Endpoints ---

    @app.get("/api/status")
    async def api_status(request: Request):
        _check_auth(request)
        return adapter.get_status_snapshot()

    @app.get("/api/prices")
    async def api_prices(request: Request):
        _check_auth(request)
        return adapter.get_price_snapshot()

    @app.get("/api/markets")
    async def api_markets(request: Request):
        _check_auth(request)
        return adapter.get_market_snapshot()

    @app.get("/api/signals")
    async def api_signals(request: Request):
        _check_auth(request)
        return adapter.get_signal_snapshot()

    @app.get("/api/positions")
    async def api_positions(request: Request):
        _check_auth(request)
        return adapter.get_positions_snapshot()

    @app.get("/api/performance")
    async def api_performance(request: Request):
        _check_auth(request)
        return adapter.get_performance_snapshot()

    @app.get("/api/risk")
    async def api_risk(request: Request):
        _check_auth(request)
        return adapter.get_risk_snapshot()

    @app.get("/api/health")
    async def api_health(request: Request):
        _check_auth(request)
        return adapter.get_health_snapshot()

    @app.post("/api/kill-switch/activate")
    async def api_kill_switch_activate(
        request: Request,
        x_confirm: Optional[str] = Header(None),
    ):
        """Activate the kill switch. Requires X-Confirm: KILL header."""
        _check_auth(request)
        if x_confirm != "KILL":
            raise HTTPException(
                status_code=400,
                detail="Missing or invalid confirmation. Send header: X-Confirm: KILL"
            )
        kill_switch.activate("Dashboard kill switch activated by operator")
        log.warning("Kill switch activated via dashboard API")
        return {"status": "activated", "reason": kill_switch.trigger_reason}

    # --- WebSocket ---

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        # Auth check for WebSocket
        if config.DASHBOARD_AUTH_TOKEN:
            token = websocket.query_params.get("token", "")
            if token != config.DASHBOARD_AUTH_TOKEN:
                await websocket.close(code=4001)
                return

        await ws_bridge.connect(websocket)
        await ws_bridge.handle_client(websocket)

    # --- Static files ---

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def start_dashboard_server(
    adapter: StateAdapter,
    kill_switch: KillSwitch,
    ws_bridge: WebSocketBridge,
) -> None:
    """Start the dashboard server as an async task."""
    import uvicorn

    app = create_app(adapter, kill_switch, ws_bridge)

    uvi_config = uvicorn.Config(
        app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_config)

    log.info(f"Dashboard server starting on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    await server.serve()
