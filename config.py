"""Central configuration for Project13."""

import os
from dotenv import load_dotenv

# Load .env BEFORE any os.getenv() calls below.
# config.py is evaluated at import time — if load_dotenv() runs later
# (e.g. in main.py:run()), the env vars won't exist yet and defaults
# will be baked in permanently.
load_dotenv()

# --- WebSocket URLs ---
# Binance WebSocket URL — configurable via .env for VPS compatibility:
#   Global:     wss://stream.binance.com:9443/ws/btcusdt@trade (may be blocked on US IPs)
#   Binance.US: wss://stream.binance.us:9443/ws/btcusdt@trade
#   Data mirror: wss://data-stream.binance.vision/ws/btcusdt@trade (public, no geo-block)
BINANCE_WS_URL = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade")
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

# --- Reconnect ---
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

# --- Feed Health ---
STALE_THRESHOLD = 5.0
TICK_RATE_LOG_INTERVAL = 10
TICK_RATE_WARN_THRESHOLD = 0.5

# --- Aggregator ---
ROLLING_WINDOW_SIZE = 300
DASHBOARD_REFRESH_INTERVAL = 0.1
WARMUP_DURATION = 7.0

# --- Volatility ---
VOLATILITY_WINDOW_SECONDS = 12.0

# --- Health Check ---
HEALTH_CHECK_DURATION = 60
HEALTH_CHECK_OUTPUT = "logs/health_check.txt"

# --- Polymarket ---
POLYMARKET_GAMMA_API_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API_URL = "https://clob.polymarket.com"
MARKET_POLL_INTERVAL = 5
ORDERBOOK_DEPTH = 5
MARKET_SLUG_PREFIXES = {
    "btc-5min": "btc-updown-5m-",
    "btc-15min": "btc-updown-15m-",
}
BITCOIN_TAG_ID = 235
NEAR_RESOLUTION_THRESHOLD = 20.0


# --- Signal Engine ---
ENABLED_STRATEGIES = ["latency_arb", "sniper"]
MIN_ACTIONABLE_EDGE = 0.05
SIGNAL_COOLDOWN_SECONDS = 15   # suppress repeated identical signals

# --- Latency Arbitrage ---
LATENCY_ARB_MIN_EDGE = 0.08
LATENCY_ARB_MIN_TIME = 45             # seconds — minimum time remaining
LATENCY_ARB_MAX_SPREAD = 0.04         # tightened from 0.15
# Momentum / urgency filters — require meaningful BTC move before latency_arb executes
LATENCY_ARB_MIN_ABS_MOVE_5S = 10.0    # USD — minimum |move| over 5 seconds
LATENCY_ARB_MIN_ABS_MOVE_10S = 15.0   # USD — minimum |move| over 10 seconds
LATENCY_ARB_MIN_ABS_MOVE_30S = 25.0   # USD — minimum |move| over 30 seconds
# Proto latency gate — require urgency + lag + disagreement together
LATENCY_ARB_REQUIRE_URGENCY = True    # master switch for proto-latency gate
LATENCY_ARB_MIN_MARKET_AGE_MS = 500   # Polymarket snapshot must be ≥Xms old (lag proxy)
LATENCY_ARB_MIN_DISAGREEMENT = 0.05   # model-market prob gap required with urgency
LATENCY_ARB_REQUIRE_FRESH_MOVE = True # require move visible in 5s or 10s, not only 30s
# Market phase rules (log_only = observe, enforce = block)
LATENCY_ARB_PHASE_MODE = "log_only"              # log_only | enforce
LATENCY_ARB_EARLY_PHASE_MIN_SECONDS = 180
LATENCY_ARB_LATE_PHASE_MAX_SECONDS = 60
LATENCY_ARB_EARLY_MIN_DISAGREEMENT = 0.08
LATENCY_ARB_LATE_REQUIRE_FRESH_5S = True
LATENCY_ARB_MIN_PRICE_MOVE = 15.0     # USD — minimum |spot - strike| to trade
LATENCY_ARB_15MIN_ENABLED = False     # False = data-only, no execution for 15min

# --- Sniper ---
SNIPER_MAX_TIME = 30           # seconds — sniper only near resolution
SNIPER_MIN_TIME = 3
SNIPER_MIN_PROBABILITY = 0.85
SNIPER_MAX_ENTRY_PRICE = 0.92
SNIPER_MAX_SPREAD = 0.10
SNIPER_MAX_SIZE = 0.15

# --- Confidence ---
CONFIDENCE_HIGH_EDGE = 0.15
CONFIDENCE_HIGH_MIN_TIME = 30
CONFIDENCE_MEDIUM_EDGE = 0.08
CONFIDENCE_MEDIUM_MIN_TIME = 10

# --- Position Sizing ---
SIZE_TIER_HIGH = 0.10
SIZE_TIER_MEDIUM = 0.07
SIZE_TIER_LOW = 0.05

# --- Execution ---
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "paper")
LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "")
# Dynamic order sizing — scales with current total equity
MAX_ORDER_SIZE_PCT = 0.08              # max single order = 8% of current total equity
MAX_ORDER_SIZE_FLOOR_USDC = 5.0        # minimum order size (prevents dust orders)
MAX_ORDER_SIZE_CEILING_USDC = 500.0    # absolute ceiling regardless of equity
MAX_ORDER_SIZE_USDC = MAX_ORDER_SIZE_CEILING_USDC  # legacy alias (used by live_trader safety gate)
MAX_SIGNAL_AGE_SECONDS = 2
MAX_ENTRIES_PER_WINDOW = 3     # max positions in same market
MAX_CONCURRENT_POSITIONS = 6   # total open positions across all markets
MIN_EXECUTION_TIME_REMAINING = 3
EXECUTION_DEDUP_SECONDS = 15   # suppress repeated execution of same signal
PAPER_SIMULATED_LATENCY_MS = 200
PAPER_SLIPPAGE_PCT = 0.01
# Paper baseline for accounting validation (delete trade log when changing this)
STARTING_CAPITAL_USDC = float(os.getenv("STARTING_CAPITAL_USDC", "100.0"))
RESOLUTION_POLL_INTERVAL_SECONDS = 5
TRADE_LOG_PATH = "logs/trade_log.jsonl"

# --- Risk Engine (Phase 5) ---

# Drawdown protection (fraction of high-water mark equity)
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.30"))

# Daily loss halt: fraction of current total equity (scales with portfolio)
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.25"))
DAILY_LOSS_RESET_HOUR_UTC = 0

# Paper risk mode: warn but continue trading (data collection); live: hard block
PAPER_RISK_WARN_ONLY = True

# Consecutive loss cooldown
MAX_CONSECUTIVE_LOSSES = 6
COOLDOWN_MINUTES = 2

# Exposure limits
MAX_TOTAL_EXPOSURE_PCT = 0.50
MAX_SINGLE_MARKET_EXPOSURE_PCT = 0.25

# Circuit breakers
VOLATILITY_CIRCUIT_BREAKER = 100.0  # price std above this pauses trading
MAX_ACCEPTABLE_LATENCY_MS = 500

# Price source reconciliation
PRICE_SOURCE_DIVERGENCE_WARN_USD = 50   # warn if USDT/USD basis exceeds this (normal: $15-50)
PRICE_SOURCE_DIVERGENCE_FAIL_USD = 100  # block signals if gap exceeds this (indicates feed error)
MARKET_DATA_STALE_WARN_SECONDS = 10     # warn if Polymarket data older than this

# Fees / edge / EV
ESTIMATED_FEE_PCT = 0.02
ESTIMATED_SLIPPAGE_PCT = 0.0025
MIN_NET_EDGE = 0.03
MIN_NET_EV = 0.03              # minimum net expected value to trade (global floor; strategies may be stricter)
# BTC 5m/15m — stricter EV floor than MIN_NET_EV (latency_arb)
SHORT_MARKET_MIN_NET_EV = 0.05
# Near-resolution sniper — stricter than latency_arb on short windows
SNIPER_MIN_NET_EV = 0.06
# Rolling-std floor (USD) applied in sniper before z-score to limit extreme probabilities
MIN_MODEL_VOLATILITY_FLOOR_USD = 0.5
# Max Binance–Coinbase spot gap (USD) for sniper; 0 = disabled
SNIPER_MAX_PRICE_SOURCE_GAP_USD = 0.0

# --- Execution Quality ---
# Paper execution simulation mode:
#   "baseline" (default) — conservative taker fill: slippage + ESTIMATED_FEE_PCT
#   "maker_first_experimental" — synthetic maker-fill simulation (not grounded in measured data)
PAPER_EXECUTION_SIM_MODE = os.getenv("PAPER_EXECUTION_SIM_MODE", "baseline")

# Fee assumptions (used in EV calculations and execution metadata)
MAKER_FEE_PCT = 0.005                 # 0.5% — assumed maker fee (not yet measured)
TAKER_FEE_PCT = 0.02                  # 2% — assumed taker fee
# Experimental maker-first settings (only active when SIM_MODE = "maker_first_experimental")
PAPER_MAKER_FILL_PROB = 0.7
ALLOW_TAKER_FALLBACK = True

# Kelly sizing
KELLY_FRACTION = 0.25          # fraction of full Kelly to use
KELLY_MAX_SIZE_PCT = 0.15      # hard cap on Kelly-suggested size

# --- Fragile Certainty / Disagreement Guard ---
DISAGREEMENT_SOFT_CAP = 0.25           # soft-cap sizing when model-market gap exceeds this
DISAGREEMENT_HARD_REJECT = 0.40        # hard reject when model-market gap exceeds this
DISAGREEMENT_REDUCED_SIZE_PCT = 0.03   # max size under soft cap
FRAGILE_CERTAINTY_MODEL_PROB = 0.85    # model prob threshold for fragile certainty
FRAGILE_CERTAINTY_MAX_MARKET_PROB = 0.60  # market prob ceiling for fragile certainty
FRAGILE_CERTAINTY_SIZE_MULTIPLIER = 0.5   # multiply recommended size by this

# Kill switch
KILL_SWITCH_ACTIVE = os.getenv("KILL_SWITCH_ACTIVE", "false").lower() == "true"
KILL_SWITCH_FEED_TIMEOUT = 30   # seconds without feed data

# Reserved for future use — not yet wired into runtime
# KILL_SWITCH_LATENCY_TIMEOUT = 60  # seconds of persistent bad latency before kill
# HEALTH_CHECK_INTERVAL_SECONDS = 10  # poll interval for health monitor loop

# Reporting
PERFORMANCE_REPORT_INTERVAL_MINUTES = 60
REPORT_OUTPUT_PATH = "logs/performance_report.txt"

# --- Web Dashboard (Phase 6) ---
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() == "true"
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "3000"))
DASHBOARD_WS_UPDATE_INTERVAL_MS = 500
DASHBOARD_AUTH_TOKEN = os.getenv("DASHBOARD_AUTH_TOKEN", "")
DASHBOARD_SIGNAL_HISTORY_LENGTH = 20
DASHBOARD_FILL_HISTORY_LENGTH = 20

# --- Testing Mode ---
# When True, lowers thresholds for paper-only testing to increase trade visibility.
# SAFETY: if TESTING_MODE is True, EXECUTION_MODE is forced to "paper".
TESTING_MODE = os.getenv("TESTING_MODE", "false").lower() == "true"

if TESTING_MODE:
    if EXECUTION_MODE != "paper":
        raise SystemExit(
            "FATAL: TESTING_MODE=true requires EXECUTION_MODE=paper. "
            "Cannot run testing mode with live execution."
        )
    LATENCY_ARB_MIN_EDGE = 0.02
    SNIPER_MIN_PROBABILITY = 0.70
    SNIPER_MAX_ENTRY_PRICE = 0.95
    CONFIDENCE_HIGH_EDGE = 0.08
    CONFIDENCE_MEDIUM_EDGE = 0.04
    MIN_ACTIONABLE_EDGE = 0.02
    MIN_NET_EDGE = 0.01
    SHORT_MARKET_MIN_NET_EV = 0.01
    SNIPER_MIN_NET_EV = 0.015

# --- Replay / Tape ---
REPLAY_TAPE_ENABLED = os.getenv("REPLAY_TAPE_ENABLED", "true").lower() == "true"
REPLAY_TAPE_PATH = "data/live_tape.jsonl"
REPLAY_TAPE_EVERY_N_TICKS = 10  # record every Nth evaluation cycle (1=every, 10=1/sec at 100ms)

# --- Missed Window Tracking ---
MISSED_WINDOW_LOG_PATH = "logs/missed_windows.jsonl"

# --- Logging ---
# LOG_LEVEL is available for future use but not read at runtime;
# logger.py defaults to DEBUG and colorizes all output.
LOG_LEVEL = "INFO"
