# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Project13 is an autonomous, low-latency Python trading system targeting inefficiencies in Polymarket BTC 5-minute and 15-minute up/down prediction markets.

## Architecture

```
feeds/          → data ingestion (binance, coinbase, polymarket, aggregator)
strategies/     → signal engine: probability_model, latency_arb, sniper, market_maker (stub)
risk/           → risk_manager, kill_switch, exposure_tracker, performance_analytics, health_monitor
execution/      → order_manager, paper_trader, live_trader, position_manager, fill_tracker
dashboard/      → FastAPI server, WebSocket bridge, state adapter, static frontend
models/         → price_tick, market_state, trade_signal, order, position
utils/          → logger, config_loader, polymarket_auth
deploy/         → startup.sh, stop.sh, README.md
main.py         → orchestrator + terminal dashboard
config.py       → all thresholds centralized
tests/          → 155 unit tests
```

## Pipeline

```
Feeds → Signal Engine → Risk Manager → Execution Engine → Position Manager
                                                      ↑
                                            Web Dashboard (read-only)
```

## Web Dashboard (Phase 6)

- FastAPI at `http://localhost:3000` (configurable)
- 6 panels: Price, Markets, Signals, Execution, Performance, Risk/Health
- WebSocket at `/ws/live` for real-time updates (500ms)
- REST API at `/api/{status,prices,markets,signals,positions,performance,risk,health}`
- Only write: `POST /api/kill-switch/activate` (requires `X-Confirm: KILL` header)
- Optional auth: set `DASHBOARD_AUTH_TOKEN` in .env
- Dashboard crash does not affect bot

## Running

```bash
python main.py              # Bot + terminal + web dashboard
python health_check.py      # 60-second diagnostic
python -m unittest discover -s tests -v  # 155 tests
./deploy/startup.sh         # Background start
./deploy/stop.sh            # Graceful stop
```

Access dashboard at `http://localhost:3000` (or `http://<vps-ip>:3000`).

## Config

- All thresholds in `config.py`, env vars for secrets/overrides
- Default: `EXECUTION_MODE=paper`, `TRADING_ENABLED=true`, `DASHBOARD_ENABLED=true`
- Live requires: `EXECUTION_MODE=live` + `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND`
- Dashboard auth: `DASHBOARD_AUTH_TOKEN=<your-token>`, access via `?token=<your-token>` in URL

## Polymarket API

- Gamma API: `gamma-api.polymarket.com/markets?active=true&closed=false&tag_id=235`
- Slugs: `btc-updown-5m-{ts}`, `btc-updown-15m-{ts}`
- Quirks: clobTokenIds/outcomes are JSON strings; outcomePrices often None; use CLOB `/midpoint`
- Token mapping: UP → up_token_id (clobTokenIds[0]), DOWN → down_token_id (clobTokenIds[1])

## Replay System (Phase 7A-lite)

Record live inputs → replay offline through same stack → export for calibration.

```bash
# Record (automatic when bot runs)
python main.py              # tape writes to data/live_tape.jsonl

# Replay
python3 -m replay --tape data/live_tape.jsonl --mode fast
python3 -m replay --tape data/live_tape.jsonl --mode realtime

# Export replay for calibration
python3 scripts/calibration_export.py --trade-log data/replay_trade_log.jsonl --signal-trace data/replay_signal_execution_trace.jsonl
```

See `replay/README.md` for details and realism gaps.

## All Phases Complete

1. Data layer — BTC feeds, failover, volatility, warmup
2. Polymarket — market discovery, orderbook, normalization
3. Signal engine — probability model, edge detection, strategies
4. Execution — paper/live trading, positions, PnL, trade persistence
5. Risk engine — drawdown/loss controls, kill switch, health monitoring, deployment
6. Web dashboard — real-time monitoring, 6-panel layout, kill switch control
7A-lite. Replay — tape capture, offline replay, calibration export integration
