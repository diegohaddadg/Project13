"""Polymarket BTC up/down market data feed.

Discovers and tracks active BTC 5-minute and 15-minute prediction markets
on Polymarket. Ingests market metadata, pricing, and orderbook depth.

Market Discovery Strategy:
    - Uses Gamma API (gamma-api.polymarket.com) to discover active markets
    - Merges: (1) GET /markets with repeated slug= for UTC anchor slugs (current ±1 period per
      grid) so the live short window is always requested; (2) paginated tag_id=235 browse
      (order=createdAt, ascending=false, limit/offset) for breadth.
    - Filters merged results by slug prefix: "btc-updown-5m-" and "btc-updown-15m-"
    - Row selection prefers the slug whose [period_start, period_end] contains now (live window),
      not merely the nearest Gamma endDate (avoids pre-window \"next\" markets).
    - Window countdown for the dashboard uses the slug embedded unix period start
      (btc-updown-5m-<ts>) plus 5m/15m — not Gamma eventStartTime alone (often wrong).
    - Polls every MARKET_POLL_INTERVAL seconds to detect market transitions

Orderbook Data:
    - Uses CLOB API (clob.polymarket.com) for orderbook depth
    - Endpoint: GET /book?token_id=X (no authentication required)
    - Captures top ORDERBOOK_DEPTH levels of bids and asks for the Up token

Pricing:
    - Gamma API outcomePrices (list response) or CLOB /midpoint as fallback
    - Outcomes: clobTokenIds[0] = "Up", clobTokenIds[1] = "Down"

Strike Price:
    - BTC up/down markets resolve based on BTC price at eventStartTime vs endDate.
    - The "strike" (price to beat) is the BTC price captured by Chainlink oracle
      at eventStartTime. This exact value is NOT in the Gamma API metadata.
    - We approximate the strike using the current BTC spot price at the time
      we first discover the market. This is valid because we only pick markets
      that have already started their time window (spot ≈ strike for fresh markets).
    - For an exact strike, the Chainlink BTC/USD data stream would need to be queried.

API quirks:
    - Gamma returns clobTokenIds and outcomes as JSON strings, not lists
    - outcomePrices may be a JSON string list, None, or a real list
    - Gamma spread field is a float (bid-ask spread in decimal)
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Optional

import aiohttp

from models.market_state import MarketState, OrderLevel
from utils.logger import get_logger
import config

log = get_logger("polymarket")

# Gamma discovery: single page of newest-created markets omitted the currently-live
# short-window BTC rows; anchor slugs + pagination fix the candidate pool upstream.
GAMMA_PAGE_LIMIT = 500
GAMMA_MAX_PAGES = 6  # paginated tag browse (anchor fetch covers the live window)

_DEBUG_POLL_LOG = "/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log"


def _dbg_poll(message: str, data: dict) -> None:
    # region agent log
    try:
        line = {
            "sessionId": "16560d",
            "hypothesisId": "PM1",
            "location": "polymarket.py:_poll_markets",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(_DEBUG_POLL_LOG, "a") as _df:
            _df.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass
    # endregion


def _parse_json_field(value) -> list:
    """Parse a field that may be a JSON string or already a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


class PolymarketFeed:
    """Polymarket BTC up/down market data ingestion."""

    def __init__(self, chainlink_feed=None):
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._active_markets: dict[str, MarketState] = {}
        self._active_condition_ids: dict[str, str] = {}
        # Strike prices captured at market discovery (BTC spot at the time)
        self._captured_strikes: dict[str, float] = {}
        # Chainlink on-chain feed for better strike approximation
        self._chainlink_feed = chainlink_feed
        # Condition IDs whose strike has been confirmed from oracle priceToBeat
        self._oracle_strike_confirmed: set[str] = set()
        # Oracle strike source per condition_id (for strike_source field)
        self._oracle_strike_source: dict[str, str] = {}
        # Timestamp when oracle strike was confirmed per condition_id
        self._oracle_strike_confirmed_ts: dict[str, float] = {}
        # Timestamp when each condition_id's window was first discovered (for timeout)
        self._window_discovered_at: dict[str, float] = {}
        # Condition IDs that have already logged the timeout_elapsed message (one-shot)
        self._approx_fallback_logged: set[str] = set()

        self.poll_count: int = 0
        self.transition_count: int = 0
        self.api_errors: int = 0

        # External spot price injection for strike capture
        self._latest_spot_price: float = 0.0
        # External USDT/USD basis gap injection for fallback evaluation
        self._latest_price_source_gap: float = 0.0
        # Strike analytics counters
        self.strike_analytics = {
            "confirmed_trades": 0,
            "approx_trades": 0,
            "timeout_skipped": 0,
            "fallback_rejected_late_discovery": 0,
            "fallback_rejected_high_gap": 0,
            "fallback_rejected_weak_edge": 0,
        }

    def set_spot_price(self, price: float) -> None:
        """Update the latest BTC spot price (called by aggregator)."""
        self._latest_spot_price = price

    def set_price_source_gap(self, gap: float) -> None:
        """Update the latest USDT/USD basis gap (called by aggregator)."""
        self._latest_price_source_gap = gap

    def _evaluate_approx_fallback(
        self,
        condition_id: str,
        market_type: str,
        slug: str,
        strike_price: float,
        discovered_at: float,
        waiting_seconds: float,
    ) -> tuple[str, str, float]:
        """Evaluate whether approximate strike is good enough for gated trading.

        Returns (strike_status, strike_source, strike_confirmed_at).

        Conditions — ALL must pass:
        A. Early discovery: market discovered within STRIKE_APPROX_MAX_DISCOVERY_DELAY_S of window open
        B. Small basis gap: USDT/USD gap <= STRIKE_APPROX_MAX_GAP_USD
        C. (Strong edge buffer is checked later in signal_engine)
        """
        if not config.STRIKE_ALLOW_APPROX_FALLBACK:
            self.strike_analytics["timeout_skipped"] += 1
            return ("timeout", "spot_approx", 0.0)

        # --- Condition A: early discovery ---
        # Discovery delay = time between slug timestamp (window start) and discovery
        window_start_ts = self._slug_to_timestamp(slug)
        if window_start_ts > 0:
            discovery_delay = discovered_at - window_start_ts
        else:
            discovery_delay = waiting_seconds  # fallback: use waiting time

        gap = self._latest_price_source_gap

        log.info(
            f"[STRIKE] fallback_eval market={market_type} "
            f"discovery_delay={discovery_delay:.1f}s gap=${gap:,.2f} "
            f"window={slug[:48]}"
        )

        if discovery_delay > config.STRIKE_APPROX_MAX_DISCOVERY_DELAY_S:
            self.strike_analytics["fallback_rejected_late_discovery"] += 1
            log.warning(
                f"[STRIKE] fallback_rejected market={market_type} "
                f"reason=late_discovery discovery_delay={discovery_delay:.1f}s "
                f"max={config.STRIKE_APPROX_MAX_DISCOVERY_DELAY_S}s"
            )
            self.strike_analytics["timeout_skipped"] += 1
            return ("timeout", "spot_approx", 0.0)

        # --- Condition B: small basis gap ---
        if gap > config.STRIKE_APPROX_MAX_GAP_USD:
            self.strike_analytics["fallback_rejected_high_gap"] += 1
            log.warning(
                f"[STRIKE] fallback_rejected market={market_type} "
                f"reason=high_gap gap=${gap:,.2f} "
                f"max=${config.STRIKE_APPROX_MAX_GAP_USD:,.2f}"
            )
            self.strike_analytics["timeout_skipped"] += 1
            return ("timeout", "spot_approx", 0.0)

        # Conditions A+B passed — allow signaling with edge buffer (condition C in signal_engine)
        return ("approx_fallback", "spot_approx_early", 0.0)

    @staticmethod
    def _slug_to_timestamp(slug: str) -> float:
        """Extract the Unix timestamp from a btc-updown slug. Returns 0 on failure."""
        match = re.match(r"^btc-updown-\d+m-(\d+)$", slug)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, TypeError):
                pass
        return 0.0

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Project13/1.0 (Polymarket feed)"},
        )
        log.info("Polymarket feed starting...")
        log.warning(
            f"[STRIKE] approx_gap_threshold_active "
            f"max_gap_usd={config.STRIKE_APPROX_MAX_GAP_USD}"
        )
        log.warning(
            f"[STRIKE] confirmation_timeout_active "
            f"timeout_s={config.STRIKE_CONFIRMATION_TIMEOUT_S}"
        )
        try:
            while self._running:
                try:
                    await self._poll_markets()
                except Exception as e:
                    self.api_errors += 1
                    log.error(f"Polymarket poll error: {e}")
                await asyncio.sleep(config.MARKET_POLL_INTERVAL)
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def stop(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("Polymarket feed stopped")

    # --- Public API ---

    def get_active_markets(self) -> dict[str, MarketState]:
        return dict(self._active_markets)

    def get_market_state(self, market_type: str) -> Optional[MarketState]:
        return self._active_markets.get(market_type)

    def get_market_price(self, market_type: str) -> Optional[tuple[float, float]]:
        state = self._active_markets.get(market_type)
        return (state.yes_price, state.no_price) if state else None

    def get_orderbook(self, market_type: str) -> Optional[tuple[list[OrderLevel], list[OrderLevel]]]:
        state = self._active_markets.get(market_type)
        return (state.orderbook_bids, state.orderbook_asks) if state else None

    def get_time_remaining(self, market_type: str) -> Optional[float]:
        state = self._active_markets.get(market_type)
        return state.time_remaining_seconds if state else None

    def get_strike_price(self, market_type: str) -> Optional[float]:
        state = self._active_markets.get(market_type)
        return state.strike_price if state else None

    # --- Discovery ---

    async def _poll_markets(self) -> None:
        self.poll_count += 1
        _dbg_poll(
            "poll_start",
            {
                "poll_count": self.poll_count,
                "active_market_keys_before": list(self._active_markets.keys()),
            },
        )
        gamma_markets = await self._fetch_gamma_markets()
        if gamma_markets is None:
            _dbg_poll("gamma_fetch_returned_none", {"poll_count": self.poll_count})
            return

        now_utc = datetime.now(timezone.utc)
        fetch_verify: dict[str, dict[str, Any]] = {}

        for market_type, slug_prefix in config.MARKET_SLUG_PREFIXES.items():
            candidates = []
            for m in gamma_markets:
                slug = m.get("slug", "")
                if not slug.startswith(slug_prefix):
                    continue
                if m.get("active") is not True or m.get("closed") is True:
                    continue
                # Do not require acceptingOrders here: Gamma often omits it for the live window row,
                # which would exclude the actually active market from selection.
                candidates.append(m)

            n_live, any_live = PolymarketFeed._live_candidate_stats(
                candidates, market_type, now_utc
            )

            if not candidates:
                if market_type in self._active_markets:
                    log.warning(f"No active {market_type} market found — previous may have resolved")
                fetch_verify[market_type] = {
                    "n_candidates": 0,
                    "n_live": 0,
                    "any_live": False,
                    "chosen_id": "",
                    "selected_slug": "",
                    "condition_id": "",
                    "timing_source": "",
                    "ttw": 0.0,
                    "tr": 0.0,
                }
                continue

            # Soonest endDate alone can be a far-future listing (~21h). Prefer minute-scale rows.
            market_data = PolymarketFeed._select_market_candidate(
                candidates, market_type, now_utc
            )
            if market_data is None:
                fetch_verify[market_type] = {
                    "n_candidates": len(candidates),
                    "n_live": n_live,
                    "any_live": any_live,
                    "chosen_id": "",
                    "selected_slug": "",
                    "condition_id": "",
                    "timing_source": "",
                    "ttw": 0.0,
                    "tr": 0.0,
                }
                continue
            condition_id = market_data.get("conditionId", "")

            # Detect market transition
            prev_condition_id = self._active_condition_ids.get(market_type)
            if condition_id != prev_condition_id:
                if prev_condition_id is not None:
                    self.transition_count += 1
                    log.info(
                        f"MARKET TRANSITION [{market_type}]: "
                        f"{prev_condition_id[:16]}... → {condition_id[:16]}... "
                        f"(transition #{self.transition_count})")
                    log.info(
                        f"  New market: {market_data.get('slug')} | "
                        f"ID: {market_data.get('id')} | "
                        f"condition_id: {condition_id}")
                else:
                    log.info(
                        f"Discovered {market_type}: {market_data.get('slug')} | "
                        f"ID: {market_data.get('id')} | "
                        f"condition_id: {condition_id}")
                self._active_condition_ids[market_type] = condition_id
                self._window_discovered_at[condition_id] = time.time()

                # Capture strike for this new market.
                # Prefer Chainlink on-chain (~$5 error) over Coinbase spot (~$50 error).
                cl_price = None
                if self._chainlink_feed:
                    cl_price = self._chainlink_feed.read_latest_price()
                if cl_price and cl_price > 0:
                    self._captured_strikes[condition_id] = cl_price
                    log.info(
                        f"  Strike captured: ${cl_price:,.2f} "
                        f"(Chainlink on-chain, ~$5 median error)"
                    )
                elif self._latest_spot_price > 0:
                    self._captured_strikes[condition_id] = self._latest_spot_price
                    log.info(
                        f"  Strike captured: ${self._latest_spot_price:,.2f} "
                        f"(Coinbase spot fallback, ~$50 error)"
                    )

            # Try to upgrade strike to real oracle priceToBeat from events API
            slug = market_data.get("slug", "")
            await self._try_update_strike_from_oracle(condition_id, slug, market_type)

            state = await self._build_market_state(market_data, market_type)
            if state:
                self._active_markets[market_type] = state
                _dbg_poll(
                    "_active_markets_write",
                    {
                        "market_type": market_type,
                        "market_id": state.market_id,
                        "slug": (state.slug or "")[:64],
                        "condition_id": (state.condition_id or "")[:24],
                    },
                )
            else:
                _dbg_poll(
                    "build_market_state_none",
                    {
                        "market_type": market_type,
                        "gamma_slug": (market_data.get("slug") or "")[:64],
                    },
                )
            fetch_verify[market_type] = {
                "n_candidates": len(candidates),
                "n_live": n_live,
                "any_live": any_live,
                "chosen_id": str(market_data.get("id", "")),
                "selected_slug": market_data.get("slug", "") or "",
                "condition_id": (state.condition_id if state else "")
                or market_data.get("conditionId", ""),
                "timing_source": state.timing_source if state else "",
                "ttw": state.time_to_window_seconds if state else 0.0,
                "tr": state.time_remaining_seconds if state else 0.0,
            }

        self._log_gamma_fetch_verification(fetch_verify)
        _dbg_poll(
            "poll_end",
            {
                "poll_count": self.poll_count,
                "active_market_keys_after": list(self._active_markets.keys()),
                "verify_summary": {
                    k: {
                        "n_candidates": v.get("n_candidates"),
                        "chosen_id": v.get("chosen_id"),
                        "slug": (v.get("selected_slug") or "")[:56],
                    }
                    for k, v in fetch_verify.items()
                },
            },
        )

    def _log_gamma_fetch_verification(self, fetch_verify: dict[str, dict[str, Any]]) -> None:
        """One compact line: candidate counts, live-in-pool, selection + timing (unchanged rules)."""

        def _one(mk: str) -> str:
            v = fetch_verify.get(mk, {})
            return (
                f"n={v.get('n_candidates', 0)} live_in_pool={v.get('any_live')} "
                f"(n_live={v.get('n_live', 0)}) id={v.get('chosen_id')} "
                f"slug={str(v.get('selected_slug', ''))[:52]} "
                f"cond={str(v.get('condition_id', ''))[:20]} "
                f"src={v.get('timing_source')} ttw={float(v.get('ttw', 0)):.1f} "
                f"tr={float(v.get('tr', 0)):.1f}"
            )

        log.info("Gamma fetch verify | btc-5min: %s | btc-15min: %s", _one("btc-5min"), _one("btc-15min"))

    @staticmethod
    def _btc_anchor_slugs(now_utc: datetime) -> list[str]:
        """UTC slug period anchors: current 5m/15m grid cell ± one step (matches btc-updown-*-<ts>)."""
        ts = int(now_utc.timestamp())
        g5, g15 = 300, 900
        c5 = (ts // g5) * g5
        c15 = (ts // g15) * g15
        ordered: list[str] = []
        seen: set[str] = set()
        for delta in (-g5, 0, g5):
            s = f"btc-updown-5m-{c5 + delta}"
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        for delta in (-g15, 0, g15):
            s = f"btc-updown-15m-{c15 + delta}"
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        return ordered

    @staticmethod
    def _live_candidate_stats(
        candidates: list, market_type: str, now_utc: datetime
    ) -> tuple[int, bool]:
        """How many rows satisfy window_started && tr>0 (same as in_slug_live_window)."""
        window_d = 300.0 if market_type == "btc-5min" else 900.0
        n = 0
        for m in candidates:
            slug = m.get("slug") or ""
            st = PolymarketFeed._slug_period_timing(slug, window_d, now_utc, market_type)
            if st is None:
                continue
            tr, _ttw, ws = st
            if ws and tr > 0:
                n += 1
        return (n, n > 0)

    async def _fetch_gamma_markets(self) -> Optional[list[dict]]:
        """Merge anchor slug lookups with paginated tag browse so the pool includes the live row."""
        now_utc = datetime.now(timezone.utc)
        url = f"{config.POLYMARKET_GAMMA_API_URL}/markets"
        by_id: dict[str, dict] = {}

        def _take_row(m: dict) -> None:
            mid = m.get("id")
            if mid is None:
                return
            by_id[str(mid)] = m

        async def _get(params) -> Optional[list]:
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        self.api_errors += 1
                        log.error(f"Gamma API returned {resp.status}")
                        return None
                    data = await resp.json()
                    if not isinstance(data, list):
                        self.api_errors += 1
                        log.error(f"Gamma API unexpected format: {type(data).__name__}")
                        return None
                    return data
            except aiohttp.ClientError as e:
                self.api_errors += 1
                log.error(f"Gamma API request failed: {e}")
                return None

        anchor_params: list[tuple[str, str]] = [
            ("active", "true"),
            ("closed", "false"),
        ]
        for s in PolymarketFeed._btc_anchor_slugs(now_utc):
            anchor_params.append(("slug", s))

        tag_page_params = {
            "active": "true",
            "closed": "false",
            "tag_id": str(config.BITCOIN_TAG_ID),
            "order": "createdAt",
            "ascending": "false",
            "limit": str(GAMMA_PAGE_LIMIT),
            "offset": "0",
        }
        anchor_data, page0 = await asyncio.gather(
            _get(anchor_params),
            _get(tag_page_params),
        )
        if anchor_data:
            for m in anchor_data:
                if isinstance(m, dict):
                    _take_row(m)
        else:
            log.warning("Gamma anchor slug fetch failed or empty — paginated tag scan only")

        if page0:
            for m in page0:
                if isinstance(m, dict):
                    _take_row(m)

        for page in range(1, GAMMA_MAX_PAGES):
            offset = page * GAMMA_PAGE_LIMIT
            page_params = {
                "active": "true",
                "closed": "false",
                "tag_id": str(config.BITCOIN_TAG_ID),
                "order": "createdAt",
                "ascending": "false",
                "limit": str(GAMMA_PAGE_LIMIT),
                "offset": str(offset),
            }
            batch = await _get(page_params)
            if batch is None:
                break
            for m in batch:
                if isinstance(m, dict):
                    _take_row(m)
            if len(batch) < GAMMA_PAGE_LIMIT:
                break

        if not by_id:
            return None
        return list(by_id.values())

    async def _build_market_state(self, data: dict, market_type: str) -> Optional[MarketState]:
        try:
            condition_id = data.get("conditionId", "")
            market_id = str(data.get("id", ""))
            slug = data.get("slug", "")
            question = data.get("question", "")
            end_date_str = data.get("endDate", "")
            event_start_str = data.get("eventStartTime", "")

            raw_clob_field = data.get("clobTokenIds")
            clob_token_ids = _parse_json_field(raw_clob_field)
            if len(clob_token_ids) < 2:
                log.error(f"Market {slug} has < 2 clobTokenIds — skipping (raw={raw_clob_field!r})")
                return None
            up_token_id = str(clob_token_ids[0]).strip()
            down_token_id = str(clob_token_ids[1]).strip()

            # Log token mapping for live debugging (compact — one line per market)
            if config.EXECUTION_MODE == "live":
                log.info(
                    f"Token map [{market_type}]: UP={up_token_id[:20]}...({len(up_token_id)}ch) "
                    f"DOWN={down_token_id[:20]}...({len(down_token_id)}ch) "
                    f"slug={slug[:40]} cond={condition_id[:16]}"
                )

            # Prices: prefer live CLOB midpoint (freshest), fall back to Gamma outcomePrices
            clob_yes, clob_no = await self._fetch_prices(up_token_id, down_token_id)
            if clob_yes > 0 and clob_no > 0 and not (clob_yes == 0.5 and clob_no == 0.5):
                yes_price = clob_yes
                no_price = clob_no
                price_source = "clob_mid"
            else:
                # CLOB returned defaults — use Gamma as fallback
                outcome_prices = _parse_json_field(data.get("outcomePrices"))
                if len(outcome_prices) >= 2:
                    try:
                        yes_price = float(outcome_prices[0])
                        no_price = float(outcome_prices[1])
                        price_source = "gamma_fallback"
                    except (ValueError, TypeError):
                        yes_price, no_price = clob_yes, clob_no
                        price_source = "clob_default"
                else:
                    yes_price, no_price = clob_yes, clob_no
                    price_source = "clob_default"

            # Orderbook
            bids, asks = await self._fetch_orderbook(up_token_id)

            # Spread: prefer Gamma bestBid/bestAsk
            best_bid = data.get("bestBid")
            best_ask = data.get("bestAsk")
            if best_bid is not None and best_ask is not None:
                try:
                    spread = float(best_ask) - float(best_bid)
                except (ValueError, TypeError):
                    spread = float(data.get("spread", 0) or 0)
            elif bids and asks:
                spread = asks[0].price - bids[0].price
            else:
                spread = float(data.get("spread", 0) or 0)

            # Window timing: slug unix ts encodes the CURRENT 5m/15m period for this contract.
            # Gamma eventStartTime often points at a future scheduled instant (~hours away) and
            # must NOT drive the dashboard countdown (misleading "observation starts in 22h").
            now_utc = datetime.now(timezone.utc)
            window_duration = 300.0 if market_type == "btc-5min" else 900.0

            # Strike: from oracle priceToBeat if available, else BTC spot at discovery.
            strike_price = self._captured_strikes.get(condition_id, 0.0)
            if strike_price <= 0 and self._latest_spot_price > 0:
                strike_price = self._latest_spot_price
                self._captured_strikes[condition_id] = strike_price

            is_oracle = condition_id in self._oracle_strike_confirmed

            # --- Strike confirmation state ---
            discovered_at = self._window_discovered_at.get(condition_id, time.time())
            waiting_seconds = time.time() - discovered_at

            if is_oracle:
                strike_status = "confirmed"
                strike_source = self._oracle_strike_source.get(condition_id, "oracle")
                strike_confirmed_at = self._oracle_strike_confirmed_ts.get(condition_id, 0.0)
            # Determine the approximate source used
            _has_chainlink = (
                self._chainlink_feed is not None
                and self._chainlink_feed.last_price > 0
            )
            _approx_source = "chainlink_onchain" if _has_chainlink else "spot_approx"

            if not config.REQUIRE_CONFIRMED_STRIKE:
                strike_status = "confirmed"
                strike_source = _approx_source
                strike_confirmed_at = 0.0
            elif waiting_seconds <= config.STRIKE_CONFIRMATION_TIMEOUT_S:
                strike_status = "waiting"
                strike_source = _approx_source
                strike_confirmed_at = 0.0
            else:
                # Timeout — evaluate approximate fallback
                strike_status, strike_source, strike_confirmed_at = self._evaluate_approx_fallback(
                    condition_id, market_type, slug, strike_price,
                    discovered_at, waiting_seconds,
                )

            # Log strike state
            spot_gap = abs(self._latest_spot_price - strike_price) if self._latest_spot_price > 0 and strike_price > 0 else 0.0
            if strike_status == "waiting":
                log.info(
                    f"[STRIKE] waiting_for_confirmed market={market_type} "
                    f"window={slug[:48]} approx_strike=${strike_price:,.2f} "
                    f"waited={waiting_seconds:.0f}s timeout={config.STRIKE_CONFIRMATION_TIMEOUT_S:.0f}s "
                    f"source=prev_finalPrice"
                )
            elif strike_status == "timeout":
                log.warning(
                    f"[STRIKE] timeout skip_window market={market_type} "
                    f"window={slug[:48]} waited={waiting_seconds:.0f}s "
                    f"approx_strike=${strike_price:,.2f} gap=${spot_gap:,.2f}"
                )
            elif strike_status == "approx_fallback":
                if condition_id not in self._approx_fallback_logged:
                    self._approx_fallback_logged.add(condition_id)
                    log.warning(
                        f"[STRIKE] timeout_elapsed_using_current_source "
                        f"market={market_type} strike_source={strike_source} "
                        f"waited={waiting_seconds:.0f}s"
                    )
                log.warning(
                    f"[STRIKE] fallback_approved market={market_type} "
                    f"window={slug[:48]} gap=${self._latest_price_source_gap:,.2f} "
                    f"discovery_delay={waiting_seconds:.0f}s "
                    f"edge_buffer_passed=true source=spot_approx_early"
                )
            else:
                log.info(
                    f"[STRIKE] confirmed market={market_type} window={slug[:48]} "
                    f"strike=${strike_price:,.2f} spot=${self._latest_spot_price:,.2f} "
                    f"gap=${spot_gap:,.2f} source={strike_source}"
                )

            log.info(
                f"[STRIKE] status market={market_type} "
                f"strike_status={strike_status} strike_source={strike_source}"
            )

            gamma_tr = self._compute_time_remaining(end_date_str)
            time_remaining, time_to_window, window_started, timing_source = (
                PolymarketFeed._derive_window_timing(
                    slug,
                    window_duration,
                    now_utc,
                    gamma_tr,
                    market_type,
                )
            )
            # region agent log
            try:
                _dbg = {
                    "sessionId": "16560d",
                    "hypothesisId": "H4",
                    "location": "polymarket.py:_build_market_state",
                    "message": "window timing derived",
                    "data": {
                        "timing_source": timing_source,
                        "rem_s": round(time_remaining, 2),
                        "ttw_s": round(time_to_window, 2),
                        "window_started": window_started,
                        "gamma_end_s": round(gamma_tr, 2),
                        "slug": slug[:48] if slug else "",
                    },
                    "timestamp": int(time.time() * 1000),
                }
                with open(
                    "/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log", "a"
                ) as _df:
                    _df.write(json.dumps(_dbg) + "\n")
            except Exception:
                pass
            # endregion

            # Signalable: market is active, has valid prices and time left in tradable window
            # When REQUIRE_CONFIRMED_STRIKE is enabled, only confirmed or approved fallback trades
            strike_ok = strike_status in ("confirmed", "approx_fallback")
            is_signalable = (
                strike_price > 0
                and yes_price > 0
                and no_price > 0
                and time_remaining > 0
                and strike_ok
            )

            return MarketState(
                market_id=market_id,
                condition_id=condition_id,
                market_type=market_type,
                strike_price=strike_price,
                yes_price=yes_price,
                no_price=no_price,
                spread=spread,
                orderbook_bids=bids,
                orderbook_asks=asks,
                time_remaining_seconds=time_remaining,
                gamma_end_remaining_seconds=gamma_tr,
                timestamp=time.time(),
                is_active=True,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                question=question,
                end_date=end_date_str,
                event_start_date=event_start_str,
                slug=slug,
                window_started=window_started,
                is_signalable=is_signalable,
                time_to_window_seconds=time_to_window,
                timing_source=timing_source,
                strike_status=strike_status,
                strike_source=strike_source,
                strike_confirmed_at=strike_confirmed_at,
            )
        except Exception as e:
            log.error(f"Failed to build MarketState for {market_type}: {e}")
            return None

    async def _fetch_prices(self, up_token_id: str, down_token_id: str) -> tuple[float, float]:
        yes_price = 0.5
        no_price = 0.5
        try:
            async with self._session.get(
                f"{config.POLYMARKET_CLOB_API_URL}/midpoint",
                params={"token_id": up_token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mid = data.get("mid")
                    if mid is not None:
                        yes_price = float(mid)
                        no_price = 1.0 - yes_price
            async with self._session.get(
                f"{config.POLYMARKET_CLOB_API_URL}/midpoint",
                params={"token_id": down_token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mid = data.get("mid")
                    if mid is not None:
                        no_price = float(mid)
        except (aiohttp.ClientError, ValueError, KeyError) as e:
            log.warning(f"CLOB price fetch failed: {e}")
        return (yes_price, no_price)

    async def _fetch_orderbook(self, token_id: str) -> tuple[list[OrderLevel], list[OrderLevel]]:
        url = f"{config.POLYMARKET_CLOB_API_URL}/book"
        try:
            async with self._session.get(url, params={"token_id": token_id}) as resp:
                if resp.status != 200:
                    return ([], [])
                data = await resp.json()
                bids = [OrderLevel(price=float(b["price"]), size=float(b["size"]))
                        for b in data.get("bids", [])[:config.ORDERBOOK_DEPTH]]
                asks = [OrderLevel(price=float(a["price"]), size=float(a["size"]))
                        for a in data.get("asks", [])[:config.ORDERBOOK_DEPTH]]
                bids.sort(key=lambda x: x.price, reverse=True)
                asks.sort(key=lambda x: x.price)
                return (bids, asks)
        except (aiohttp.ClientError, KeyError, ValueError) as e:
            log.warning(f"CLOB orderbook error: {e}")
            return ([], [])

    async def _try_update_strike_from_oracle(
        self, condition_id: str, slug: str, market_type: str
    ) -> None:
        """Try to replace the approximate strike with the real oracle priceToBeat.

        Checks two sources (in order):
        1. priceToBeat on the CURRENT window's event — available ~150s after close
        2. finalPrice on the PREVIOUS window's event — available ~320-385s after
           prev close (= ~20-85s after current close). Since priceToBeat[N] ==
           finalPrice[N-1], this is the same value via a different path.

        Timing reality for 5-minute BTC up/down markets:
        - True oracle strike is the Chainlink Data Streams BTC/USD price at
          eventStartTime (window open).
        - Polymarket does NOT publish this value until AFTER the window closes.
        - priceToBeat appears on the events API ~150s after window close.
        - finalPrice of previous window appears ~320-385s after prev close.
        - For a 5-min window, NEITHER source is available during trading.
        - For a 15-min window, the oracle strike arrives ~5-7 min in (usable).
        - Until oracle arrives, we use Coinbase BTC/USD spot at market discovery
          as an approximation (typical error: $4-50 depending on discovery lag).

        This method is idempotent: once oracle strike is confirmed, further polls
        are no-ops for this condition_id.
        """
        if condition_id in self._oracle_strike_confirmed:
            return
        if not slug or not self._session or self._session.closed:
            return

        oracle_strike = None
        source = ""

        # --- Source 1: priceToBeat on the current window's event ---
        url = f"{config.POLYMARKET_GAMMA_API_URL}/events"
        try:
            async with self._session.get(
                url, params={"slug": slug, "limit": "1"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        em = data[0].get("eventMetadata")
                        if isinstance(em, dict):
                            ptb = em.get("priceToBeat")
                            if ptb is not None:
                                oracle_strike = float(ptb)
                                source = "events_api_priceToBeat"
        except Exception:
            pass

        # --- Source 2: finalPrice of the previous window ---
        if oracle_strike is None:
            prev_slug = self._prev_window_slug(slug)
            if prev_slug:
                try:
                    async with self._session.get(
                        url, params={"slug": prev_slug, "limit": "1"}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and data:
                                em = data[0].get("eventMetadata")
                                if isinstance(em, dict):
                                    fp = em.get("finalPrice")
                                    if fp is not None:
                                        oracle_strike = float(fp)
                                        source = f"events_api_finalPrice_prev({prev_slug[-10:]})"
                except Exception:
                    pass

        # --- Source 3: Chainlink on-chain feed (better approximation, not confirmation) ---
        if oracle_strike is None and self._chainlink_feed:
            cl_price = self._chainlink_feed.read_latest_price()
            if cl_price and cl_price > 0:
                old = self._captured_strikes.get(condition_id, 0.0)
                if old > 0 and abs(cl_price - old) > 1.0:
                    self._captured_strikes[condition_id] = cl_price
                    log.info(
                        f"[STRIKE] chainlink_refresh market={market_type} "
                        f"old=${old:,.2f} new=${cl_price:,.2f} "
                        f"delta=${abs(cl_price - old):,.2f} "
                        f"age={self._chainlink_feed.price_age_seconds:.0f}s"
                    )
            # Chainlink is NOT a confirmation — don't mark confirmed, just return
            return

        if oracle_strike is None or oracle_strike <= 0:
            return

        old_strike = self._captured_strikes.get(condition_id, 0.0)
        gap = abs(oracle_strike - old_strike) if old_strike > 0 else 0.0
        self._captured_strikes[condition_id] = oracle_strike
        self._oracle_strike_confirmed.add(condition_id)
        now = time.time()
        self._oracle_strike_confirmed_ts[condition_id] = now
        # Map source to strike_source enum value
        if "finalPrice" in source:
            self._oracle_strike_source[condition_id] = "prev_finalPrice"
        elif "priceToBeat" in source:
            self._oracle_strike_source[condition_id] = "oracle"
        else:
            self._oracle_strike_source[condition_id] = "oracle"

        discovered_at = self._window_discovered_at.get(condition_id, now)
        delay = now - discovered_at

        log.warning(
            f"[STRIKE] confirmed value=${oracle_strike:,.2f} "
            f"source={self._oracle_strike_source[condition_id]} "
            f"delay={delay:.1f}s market={market_type} "
            f"slug={slug[:48]} approx_was=${old_strike:,.2f} gap=${gap:,.2f}"
        )

    @staticmethod
    def _prev_window_slug(slug: str) -> str:
        """Derive the previous window's slug from the current one.

        btc-updown-5m-1774650300 -> btc-updown-5m-1774650000
        btc-updown-15m-1774650000 -> btc-updown-15m-1774649100
        """
        match = re.match(r"^(btc-updown-(\d+)m-)(\d+)$", slug)
        if not match:
            return ""
        prefix, minutes_str, ts_str = match.group(1), match.group(2), match.group(3)
        try:
            ts = int(ts_str)
            window_seconds = int(minutes_str) * 60
            return f"{prefix}{ts - window_seconds}"
        except (ValueError, TypeError):
            return ""

    @staticmethod
    def _parse_strike_price(data: dict) -> float:
        """Legacy strike parser — no longer used. Strike is now set from BTC spot."""
        return 0.0

    @staticmethod
    def _seconds_until_end_date(m: dict, now_utc: datetime) -> float:
        """Seconds until Gamma endDate for this market row (same clock as dashboard countdown)."""
        ed = m.get("endDate", "")
        if not ed:
            return float("inf")
        try:
            end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            return max(0.0, (end_dt - now_utc).total_seconds())
        except (ValueError, TypeError):
            return float("inf")

    @staticmethod
    def _select_market_candidate(
        candidates: list, market_type: str, now_utc: datetime
    ) -> Optional[dict]:
        """Pick the Gamma row for dashboard timing.

        Active live row (source of truth for countdown): slug `btc-updown-(5m|15m)-<ts>` defines
        period [ts, ts+window]. Prefer any candidate with now inside that interval (observation
        window open, time left > 0). Otherwise fall back to nearest endDate tiers (upcoming).

        This avoids choosing a pre-window \"next\" market just because its endDate is soonest.
        """
        if not candidates:
            return None
        window_d = 300.0 if market_type == "btc-5min" else 900.0
        near_horizon = 2.0 * window_d + 120.0

        def seu(m: dict) -> float:
            return PolymarketFeed._seconds_until_end_date(m, now_utc)

        # 1) Prefer row whose slug period contains now (live window — not merely nearest endDate)
        in_live: list[tuple[float, dict]] = []
        for m in candidates:
            slug = m.get("slug") or ""
            st = PolymarketFeed._slug_period_timing(
                slug, window_d, now_utc, market_type
            )
            if st is None:
                continue
            tr, _ttw, ws = st
            if ws and tr > 0:
                in_live.append((tr, m))
        if in_live:
            in_live.sort(key=lambda x: x[0])
            chosen, branch = in_live[0][1], "in_slug_live_window"
        else:
            ordered = sorted(candidates, key=seu)
            chosen = None
            branch = ""
            for m in ordered:
                s = seu(m)
                if 0 < s <= near_horizon:
                    chosen, branch = m, "within_2w_plus_buffer"
                    break
            if chosen is None:
                for m in ordered:
                    s = seu(m)
                    if 0 < s <= 3600.0:
                        chosen, branch = m, "within_1h"
                        break
            if chosen is None:
                chosen, branch = ordered[0], "soonest_endDate_fallback"
        # region agent log
        try:
            _dbg = {
                "sessionId": "16560d",
                "hypothesisId": "H1",
                "location": "polymarket.py:_select_market_candidate",
                "message": "picked market row for countdown",
                "data": {
                    "market_type": market_type,
                    "near_horizon_s": round(near_horizon, 1),
                    "chosen_seu_s": round(seu(chosen), 2),
                    "slug": (chosen.get("slug") or "")[:56],
                    "branch": branch,
                },
                "timestamp": int(time.time() * 1000),
            }
            with open(
                "/Users/diegohaddad/Desktop/Project13/.cursor/debug-16560d.log", "a"
            ) as _df:
                _df.write(json.dumps(_dbg) + "\n")
        except Exception:
            pass
        # endregion
        return chosen

    @staticmethod
    def _compute_time_remaining(end_date_str: str) -> float:
        if not end_date_str:
            return 0.0
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            remaining = (end_dt - now).total_seconds()
            return max(0.0, remaining)
        except (ValueError, TypeError) as e:
            log.warning(f"Failed to parse end_date '{end_date_str}': {e}")
            return 0.0

    @staticmethod
    def _slug_period_timing(
        slug: str, window_duration: float, now_utc: datetime, market_type: str
    ) -> Optional[tuple[float, float, bool]]:
        """Period [start, start+window] from slug. Must use full prefix: a bare (5m|15m)- regex
        can match '5m-' inside '15m-', and production slugs need explicit btc-updown-5m- / 15m-.
        """
        if market_type == "btc-5min":
            pat = r"btc-updown-5m-(\d{8,20})"
        elif market_type == "btc-15min":
            pat = r"btc-updown-15m-(\d{8,20})"
        else:
            return None
        m = re.search(pat, slug or "", re.IGNORECASE)
        if not m:
            return None
        ts = int(m.group(1))
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        period_start = datetime.fromtimestamp(ts, tz=timezone.utc)
        period_end = period_start + timedelta(seconds=window_duration)
        time_remaining = max(0.0, (period_end - now_utc).total_seconds())
        time_to_window = max(0.0, (period_start - now_utc).total_seconds())
        window_started = now_utc >= period_start
        return (time_remaining, time_to_window, window_started)

    @staticmethod
    def _derive_window_timing(
        slug: str,
        window_duration: float,
        now_utc: datetime,
        gamma_tr: float,
        market_type: str,
    ) -> tuple[float, float, bool, str]:
        """Live countdown: (1) slug btc-updown-5m/15m-<ts> + window, or min with endDate if endDate
        is sooner; (2) else Gamma endDate for this market row. Never use eventStartTime — it
        often implies a far-future scheduled instant and produced bogus ~22h dashboard times."""
        slug_t = PolymarketFeed._slug_period_timing(
            slug, window_duration, now_utc, market_type
        )
        if slug_t is not None:
            tr_s, ttw_s, ws_s = slug_t
            # endDate on this row is authoritative for resolution; cap slug when slug encodes wrong cycle
            if gamma_tr > 0:
                tr_s = min(tr_s, gamma_tr)
            return (tr_s, ttw_s, ws_s, "slug_period")
        if gamma_tr > 0:
            return (gamma_tr, 0.0, True, "gamma_end_date")
        return (0.0, 0.0, False, "none")
