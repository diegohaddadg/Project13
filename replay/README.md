# Project13 Replay — Live Tape Capture + Offline Replay

## Overview

The replay system records the live bot's actual inputs and replays them
offline through the same signal/risk/execution stack. This accelerates
paper-strategy analysis without waiting for future live sessions.

## Workflow

### 1. Record live tape

The live bot automatically records when `REPLAY_TAPE_ENABLED=true` (default).

```bash
python main.py   # tape records to data/live_tape.jsonl
```

Config:
- `REPLAY_TAPE_ENABLED = true/false`
- `REPLAY_TAPE_PATH = "data/live_tape.jsonl"`
- `REPLAY_TAPE_EVERY_N_TICKS = 10` (1 record per ~1 second at 100ms loop)

### 2. Replay tape offline

```bash
# Fast replay (no delays, processes entire tape instantly)
python3 -m replay --tape data/live_tape.jsonl --mode fast

# Realtime replay (original speed)
python3 -m replay --tape data/live_tape.jsonl --mode realtime

# Fast replay at 10x speed
python3 -m replay --tape data/live_tape.jsonl --mode realtime --speed 10
```

Outputs:
- `data/replay_trade_log.jsonl`
- `data/replay_signal_execution_trace.jsonl`

### 3. Run calibration export on replay logs

```bash
python3 scripts/calibration_export.py \
    --trade-log data/replay_trade_log.jsonl \
    --signal-trace data/replay_signal_execution_trace.jsonl \
    --output-dir data/
```

## What is recorded in the tape

Each JSONL line contains:
- `ts` — timestamp
- `spot_price` — model spot (Coinbase USD preferred)
- `spot_source` — "coinbase_usd" or "binance_usdt"
- `volatility` — rolling std from aggregator
- `vol_source` — matching volatility source
- `price_source_gap` — USDT vs USD gap
- `feed_healthy` — whether feeds are healthy
- `market_state_5m` / `market_state_15m` — compact market snapshots:
  - market_id, condition_id, market_type
  - strike_price, yes_price, no_price, spread
  - time_remaining_seconds, window_started, is_signalable
  - timing_source, slug

## What replay reuses from live

- `strategies.signal_engine.SignalEngine` — same signal generation
- `strategies.probability_model` — same probability math
- `strategies.latency_arb` / `sniper` — same strategies
- `risk.risk_manager.RiskManager` — same risk checks
- `execution.order_manager.OrderManager` — same execution flow
- `execution.position_manager.PositionManager` — same position tracking
- Paper fill logic (same slippage, same fill simulation)

## Realism gaps

1. **Feed health**: Replay assumes healthy feeds by default.
   Taped `feed_healthy` is available but not deeply tested.

2. **Resolution timing**: In fast mode, positions resolve when tape-time
   elapsed exceeds the market window (300s for 5min, 900s for 15min).
   This is approximate — real resolution depends on market cycling.

3. **Volatility**: Uses the pre-computed volatility from the tape snapshot,
   not recalculated from raw ticks. This is the same value the live bot used.

4. **Signal cooldowns**: In fast mode, cooldown timers use tape timestamps.
   The cooldown window (15s) may trigger differently than in live.

5. **Market state**: Replayed from tape snapshots which update every
   `REPLAY_TAPE_EVERY_N_TICKS` evaluations. Market changes between
   recordings are not captured.

6. **Order fills**: Paper fills use snapshot market prices, not a
   separate simulated orderbook.

## Files

- `replay/tape_recorder.py` — records live inputs to JSONL
- `replay/replay_runner.py` — processes tape through live stack
- `replay/cli.py` — CLI interface
- `replay/__main__.py` — allows `python3 -m replay`
- `replay/README.md` — this file
