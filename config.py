"""Central configuration for Project13."""

import os
import sys
from dotenv import load_dotenv

# Detect pytest: when running under pytest, skip .env loading so tests
# use hardcoded defaults and are not poisoned by the production .env.
_RUNNING_UNDER_PYTEST = "_pytest" in sys.modules

if not _RUNNING_UNDER_PYTEST:
    # Load .env BEFORE any os.getenv() calls below.
    # config.py is evaluated at import time — if load_dotenv() runs later
    # (e.g. in main.py:run()), the env vars won't exist yet and defaults
    # will be baked in permanently.
    load_dotenv()


def _env(key: str, default: str) -> str:
    """Read env var, but always return the default when running under pytest.

    This prevents production env vars (from .env or shell exports on the
    droplet) from leaking into test runs and breaking paper-mode assumptions.
    """
    if _RUNNING_UNDER_PYTEST:
        return default
    return os.getenv(key, default)

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
ENABLED_STRATEGIES = ["latency_arb"]  # sniper disabled — live postmortem showed -$10 on 1 trade, model mispriced $0.01 token at 91%
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

# --- Latency Arb V2a Live Gates ---
# Stricter filters based on live postmortem: confident-but-wrong trades and
# rapid direction flipping were the top failure modes.
LAT_ARB_V2A_ENABLED = _env("LAT_ARB_V2A_ENABLED", "false").lower() == "true"
LAT_ARB_V2A_MAX_DISAGREEMENT = 0.30      # reject if |model_prob - market_prob| exceeds this
LAT_ARB_V2A_DIRECTION_COOLDOWN_S = 120   # seconds before allowing opposite-direction trade

# --- Latency Arb V2 (refinement layer) ---
LATENCY_ARB_V2_ENABLED = os.getenv("LATENCY_ARB_V2_ENABLED", "false").lower() == "true"

# Price-quality zones (purchased-side price thresholds)
V2_PRICE_ZONE_A_MAX = 0.52            # favorable: <= this
V2_PRICE_ZONE_B_MAX = 0.62            # acceptable: <= this
V2_PRICE_ZONE_C_MAX = 0.72            # expensive: <= this  (above = zone D)

# Continuous price score anchors
V2_PRICE_SCORE_BEST = 0.35            # score=1.0 at this price or below
V2_PRICE_SCORE_WORST = 0.80           # score=0.0 at this price or above

# Adaptive disagreement minimums by price zone
V2_DISAGREE_MIN_ZONE_A = 0.04         # favorable price tolerates weaker disagreement
V2_DISAGREE_MIN_ZONE_B = 0.05         # baseline
V2_DISAGREE_MIN_ZONE_C = 0.07         # expensive requires stronger disagreement
V2_DISAGREE_MIN_ZONE_D = 0.10         # very expensive requires very strong disagreement

# Overlap / conflict penalties
V2_OVERLAP_HIGH_THRESHOLD = 3         # start penalizing after this many open positions
V2_OVERLAP_PER_EXCESS_PENALTY = 0.08  # penalty per excess position above threshold
V2_CONFLICT_OPPOSITE_PENALTY = 0.15   # penalty per opposite-direction position in same market
V2_OVERLAP_SAME_DIR_PENALTY = 0.03    # mild penalty per same-direction stack
V2_OVERLAP_REDUCE_THRESHOLD = 0.20    # overlap penalty above this triggers size reduction

# Composite quality weights (sum should ≈ 1.0)
V2_WEIGHT_PRICE = 0.30
V2_WEIGHT_DISAGREEMENT = 0.25
V2_WEIGHT_URGENCY = 0.10
V2_WEIGHT_FRESHNESS = 0.10
V2_WEIGHT_EV = 0.25

# Decision thresholds per zone
V2_ZONE_D_EXCEPTIONAL_THRESHOLD = 0.75   # quality must exceed this to override zone D reject
V2_ZONE_D_EXCEPTIONAL_SIZE_MULT = 0.30   # still heavily reduced even if exceptional
V2_ZONE_C_MIN_QUALITY = 0.35             # below this → reject in zone C
V2_ZONE_C_FULL_QUALITY = 0.55            # above this → approve full size in zone C
V2_ZONE_C_REDUCED_SIZE_MULT = 0.50       # size multiplier for borderline zone C
V2_ZONE_B_MIN_QUALITY = 0.25             # below this → reduce in zone B
V2_ZONE_B_WEAK_DISAGREE_SIZE_MULT = 0.65 # zone B with weak disagreement
V2_ZONE_B_LOW_QUALITY_SIZE_MULT = 0.70   # zone B with low quality
V2_ZONE_A_WEAK_DISAGREE_SIZE_MULT = 0.80 # zone A with weak disagreement (mild cut)
V2_ZONE_A_OVERLAP_MIN_SIZE_MULT = 0.40  # floor for overlap-based size reduction in zone A

# Composite score internals
V2_DISAGREE_SURPLUS_NORMALIZER = 0.15   # disagreement surplus of this = perfect score
V2_URGENCY_FAIL_SCORE = 0.3            # urgency_pass=False contributes this (not 0)
V2_FRESHNESS_FAIL_SCORE = 0.4          # freshness_pass=False contributes this (not 0)
V2_EV_NORMALIZER = 0.10                # net_ev at this value = perfect score

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
TRADING_ENABLED = _env("TRADING_ENABLED", "true").lower() == "true"
# FORCED PAPER MODE — live trading paused. To re-enable live, remove the
# override below and let _env("EXECUTION_MODE", "paper") read from .env.
EXECUTION_MODE = "paper"  # was: _env("EXECUTION_MODE", "paper")
LIVE_TRADING_CONFIRMATION = ""  # cleared — live requires I_UNDERSTAND
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
STARTING_CAPITAL_USDC = float(_env("STARTING_CAPITAL_USDC", "100.0"))
RESOLUTION_POLL_INTERVAL_SECONDS = 5
TRADE_LOG_PATH = "logs/trade_log.jsonl"

# --- Live Reconciliation ---
LIVE_RECONCILIATION_ENABLED = os.getenv("LIVE_RECONCILIATION_ENABLED", "true").lower() == "true"
LIVE_AUTO_REDEEM_ENABLED = os.getenv("LIVE_AUTO_REDEEM_ENABLED", "true").lower() == "true"
LIVE_RECONCILE_INTERVAL_SECONDS = float(os.getenv("LIVE_RECONCILE_INTERVAL_SECONDS", "5.0"))
LIVE_REDEEM_RETRY_BACKOFF_SECONDS = 30.0
LIVE_REDEEM_MAX_RETRIES = 5

# Phase 2a: When True, the bot enqueues redeem candidates to
# data/redeem_queue.jsonl instead of submitting on-chain tx inline.
# The separate redeem worker (scripts/run_redeem_worker.py) processes
# the queue.  Requires LIVE_AUTO_REDEEM_ENABLED=True to reach the
# redeem scan at all.  Set to False to revert to inline redeem.
LIVE_REDEEM_ENQUEUE_ONLY = os.getenv("LIVE_REDEEM_ENQUEUE_ONLY", "false").lower() == "true"

# --- Paper-Like Risk Mode (Strategy B) ---
# When enabled, risk/sizing/exposure calculations use a synthetic baseline
# instead of actual restored equity. This makes a small live account behave
# like a $100 paper account for entry aggressiveness, while keeping
# accounting/reconciliation/dashboard based on real equity.
PAPER_LIKE_RISK_MODE = os.getenv("PAPER_LIKE_RISK_MODE", "false").lower() == "true"
PAPER_LIKE_BASELINE_USDC = float(os.getenv("PAPER_LIKE_BASELINE_USDC", "100.0"))

# --- Risk Engine (Phase 5) ---

# Drawdown protection (fraction of high-water mark equity)
MAX_DRAWDOWN_PCT = float(_env("MAX_DRAWDOWN_PCT", "0.30"))
DRAWDOWN_COOLDOWN_SECONDS = float(os.getenv("DRAWDOWN_COOLDOWN_SECONDS", "300"))  # 5 minutes

# Daily loss halt: fraction of current total equity (scales with portfolio)
DAILY_LOSS_LIMIT_PCT = float(_env("DAILY_LOSS_LIMIT_PCT", "0.25"))
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
KILL_SWITCH_ACTIVE = _env("KILL_SWITCH_ACTIVE", "false").lower() == "true"
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

# --- Strike Confirmation ---
# When True, markets with unconfirmed strikes are not signalable (no trading).
# The oracle strike arrives via events API after the previous window resolves.
REQUIRE_CONFIRMED_STRIKE = _env("REQUIRE_CONFIRMED_STRIKE", "true").lower() == "true"
STRIKE_CONFIRMATION_TIMEOUT_S = float(_env("STRIKE_CONFIRMATION_TIMEOUT_S", "15"))

# --- Approximate Strike Fallback ---
# When confirmed strike does not arrive in time, allow approximate strike
# under strict conditions: early discovery, small basis gap, strong edge.
STRIKE_ALLOW_APPROX_FALLBACK = _env("STRIKE_ALLOW_APPROX_FALLBACK", "true").lower() == "true"
STRIKE_APPROX_MAX_DISCOVERY_DELAY_S = float(_env("STRIKE_APPROX_MAX_DISCOVERY_DELAY_S", "30"))
STRIKE_APPROX_MAX_GAP_USD = float(_env("STRIKE_APPROX_MAX_GAP_USD", "70"))
STRIKE_APPROX_EDGE_MULTIPLIER = float(_env("STRIKE_APPROX_EDGE_MULTIPLIER", "2.0"))

# --- Chainlink On-Chain Strike Source ---
# Read Chainlink BTC/USD price feed on Polygon as an early strike estimate.
# Not identical to Data Streams (which Polymarket uses for resolution), but
# validated to ~$3 median / ~$5 mean error vs priceToBeat across 22 windows.
# Used as strike_source="chainlink_onchain" — better than Coinbase spot (~$50 error).
STRIKE_CHAINLINK_ENABLED = _env("STRIKE_CHAINLINK_ENABLED", "true").lower() == "true"
STRIKE_CHAINLINK_RPC_URL = _env("STRIKE_CHAINLINK_RPC_URL", "https://rpc-mainnet.matic.quiknode.pro")
# Chainlink BTC/USD aggregator proxy on Polygon mainnet
STRIKE_CHAINLINK_AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

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
