#!/usr/bin/env python3
"""Analyze the current Project13 VPS run from live log files.

Usage:
    python3 scripts/analyze_vps_current_run.py

Reads only from the repo working directory (expected: /root/Project13).
Does NOT modify any runtime files.
Writes summary to stdout and logs/vps_current_run_analysis.txt.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (relative to repo root)
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "logs"
DATA = REPO / "data"

TRADE_LOG = LOGS / "trade_log.jsonl"
FILL_TRACE = LOGS / "fill_to_position_trace.jsonl"
PERF_LATEST = LOGS / "performance_report_latest.txt"
OUTPUT = LOGS / "vps_current_run_analysis.txt"


def find_signal_traces():
    """Return all signal_execution_trace JSONL files sorted newest-first."""
    traces = sorted(LOGS.glob("signal_execution_trace*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return traces


def load_jsonl(path, max_lines=500_000):
    """Load JSONL file, return list of dicts. Tolerant of corrupt lines."""
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                pass
    return rows


def ts_str(ts):
    """Unix timestamp → human-readable UTC string."""
    if not ts:
        return "?"
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Step 1: Detect current run boundary
# ---------------------------------------------------------------------------
def detect_run_boundary():
    """Determine the current run's start time and run_id.

    Strategy (in priority order):
      1. systemd service start time (systemctl show project13)
      2. Latest signal_execution_trace run_id / run_started_at
      3. Most recent equity reset in dashboard_truth_trace
      4. Fallback: trade_log.jsonl mtime
    """
    info = {
        "method": None,
        "run_id": None,
        "start_ts": None,
        "start_str": None,
        "trace_file": None,
        "caveats": [],
    }

    # --- Method 1: systemd ---
    try:
        result = subprocess.run(
            ["systemctl", "show", "project13", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "ActiveEnterTimestamp=" in result.stdout:
            raw = result.stdout.strip().split("=", 1)[1].strip()
            if raw:
                # Parse systemd timestamp (e.g. "Mon 2026-03-24 03:43:05 UTC")
                # Try multiple formats
                for fmt in ["%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"]:
                    try:
                        dt = datetime.strptime(raw, fmt)
                        info["start_ts"] = dt.replace(tzinfo=timezone.utc).timestamp()
                        info["start_str"] = raw
                        info["method"] = "systemd"
                        break
                    except ValueError:
                        continue
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # --- Method 2: Latest signal execution trace ---
    traces = find_signal_traces()
    if traces:
        latest_trace = traces[0]
        rows = load_jsonl(latest_trace, max_lines=5)
        if rows:
            first = rows[0]
            run_id = first.get("run_id")
            run_started = first.get("run_started_at")
            info["run_id"] = run_id
            info["trace_file"] = str(latest_trace.name)
            if run_started:
                # If systemd didn't work or trace is newer, prefer trace
                if info["start_ts"] is None or abs(run_started - (info["start_ts"] or 0)) < 300:
                    info["start_ts"] = run_started
                    info["start_str"] = ts_str(run_started)
                    if info["method"] is None:
                        info["method"] = "signal_trace"
                elif run_started > (info["start_ts"] or 0):
                    # Trace is from a restart after systemd start
                    info["start_ts"] = run_started
                    info["start_str"] = ts_str(run_started)
                    info["method"] = "signal_trace (restart detected)"
                    info["caveats"].append(
                        "Signal trace run_started_at is newer than systemd start — "
                        "bot may have restarted within the service."
                    )

    # --- Method 3: Fallback to trade_log mtime ---
    if info["start_ts"] is None and TRADE_LOG.exists():
        mtime = TRADE_LOG.stat().st_mtime
        info["start_ts"] = mtime - 3600  # rough guess: 1 hour before last write
        info["start_str"] = f"~{ts_str(mtime)} (trade_log mtime fallback)"
        info["method"] = "trade_log_mtime_fallback"
        info["caveats"].append(
            "Could not determine exact run start. Using trade_log.jsonl mtime minus 1 hour as rough estimate."
        )

    if info["start_ts"] is None:
        info["method"] = "none"
        info["caveats"].append("Could not determine run boundary from any source.")

    return info


# ---------------------------------------------------------------------------
# Step 2: Load and filter trade log
# ---------------------------------------------------------------------------
def load_trade_log_deduped(since_ts=None):
    """Load trade_log.jsonl, dedup by order_id (last line wins), optionally filter by time."""
    raw = load_jsonl(TRADE_LOG)
    if not raw:
        return []

    # Dedup: last line per order_id is canonical
    by_oid = {}
    for i, row in enumerate(raw):
        oid = str(row.get("order_id") or "").strip()
        key = oid if oid else f"__missing_{i}__"
        by_oid[key] = row

    orders = sorted(by_oid.values(), key=lambda d: float(d.get("timestamp") or 0))

    if since_ts:
        orders = [o for o in orders if float(o.get("timestamp") or 0) >= since_ts]

    return orders


# ---------------------------------------------------------------------------
# Step 3: Load signal execution traces for the current run
# ---------------------------------------------------------------------------
def load_signal_traces(run_id=None, since_ts=None):
    """Load signal execution trace rows filtered by run_id or timestamp."""
    all_rows = []
    for path in find_signal_traces():
        rows = load_jsonl(path)
        all_rows.extend(rows)

    if run_id:
        filtered = [r for r in all_rows if r.get("run_id") == run_id]
        if filtered:
            return filtered

    if since_ts:
        return [r for r in all_rows if (r.get("ts") or 0) >= since_ts]

    return all_rows


# ---------------------------------------------------------------------------
# Step 4: Load fill trace
# ---------------------------------------------------------------------------
def load_fill_trace(since_ts=None):
    """Load fill_to_position_trace.jsonl, optionally filtered by time."""
    rows = load_jsonl(FILL_TRACE)
    if since_ts:
        rows = [r for r in rows if (r.get("ts") or 0) >= since_ts]
    return rows


# ---------------------------------------------------------------------------
# Step 5: Parse latest performance report
# ---------------------------------------------------------------------------
def parse_perf_report():
    """Extract key metrics from performance_report_latest.txt."""
    if not PERF_LATEST.exists():
        return None
    content = PERF_LATEST.read_text()
    result = {}

    patterns = {
        "timestamp": r"Timestamp:\s+(.+)",
        "session_min": r"Session:\s+([\d.]+)",
        "capital": r"Capital:\s+\$([\d.]+)",
        "total_trades": r"Total:\s+(\d+)",
        "wins": r"Wins:\s+(\d+)",
        "losses": r"Losses:\s+(\d+)",
        "win_rate": r"Win Rate:\s+([\d.]+)%",
        "pnl_total": r"Total:\s+\$([+-]?[\d.]+)",
        "avg_pnl": r"Average:\s+\$([+-]?[\d.]+)",
        "best_trade": r"Best Trade:\s+\$([+-]?[\d.]+)",
        "worst_trade": r"Worst Trade:\s+\$([+-]?[\d.]+)",
        "gross_wins": r"Gross Wins:\s+\$([\d.]+)",
        "gross_losses": r"Gross Losses:\s+\$([\d.]+)",
        "profit_factor": r"Profit Factor:\s*([\d.inf]+)",
        "sharpe": r"Sharpe:\s+([+-]?[\d.]+)",
        "max_drawdown": r"Max Drawdown:\s+([\d.]+)%",
        "hwm": r"HWM:\s+\$([\d.]+)",
        "avg_hold": r"Avg Hold:\s+(\d+)s",
    }

    for key, pat in patterns.items():
        m = re.search(pat, content)
        if m:
            result[key] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# Step 6: Compute metrics
# ---------------------------------------------------------------------------
def compute_trade_metrics(orders):
    """Compute core metrics from a list of deduped order dicts."""
    filled = [o for o in orders if o.get("status") == "FILLED"]
    resolved = [o for o in filled if o.get("pnl") is not None]

    if not resolved:
        return {"total_filled": len(filled), "total_resolved": 0}

    pnls = [float(o["pnl"]) for o in resolved]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    sizes = [float(o.get("size_usdc") or 0) for o in filled]

    gross_w = sum(winners)
    gross_l = abs(sum(losers))

    # Strategy breakdown
    by_strat = defaultdict(list)
    for o in resolved:
        strat = (o.get("metadata") or {}).get("strategy", "unknown")
        by_strat[strat].append(float(o["pnl"]))

    # Market type breakdown
    by_mkt = defaultdict(list)
    for o in resolved:
        by_mkt[o.get("market_type", "unknown")].append(float(o["pnl"]))

    # Direction breakdown
    by_dir = defaultdict(list)
    for o in resolved:
        by_dir[o.get("direction", "unknown")].append(float(o["pnl"]))

    # Sizing analysis
    at_50_cap = sum(1 for s in sizes if s >= 49.5)
    at_new_ceiling = sum(1 for s in sizes if s >= 499.5)

    result = {
        "total_filled": len(filled),
        "total_resolved": len(resolved),
        "unresolved": len(filled) - len(resolved),
        "total_pnl": sum(pnls),
        "win_count": len(winners),
        "loss_count": len(losers),
        "win_rate": len(winners) / len(resolved) if resolved else 0,
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
        "avg_pnl": sum(pnls) / len(pnls),
        "avg_winner": sum(winners) / len(winners) if winners else 0,
        "avg_loser": sum(losers) / len(losers) if losers else 0,
        "largest_win": max(pnls) if pnls else 0,
        "largest_loss": min(pnls) if pnls else 0,
        "gross_wins": gross_w,
        "gross_losses": gross_l,
        "strategy_breakdown": {},
        "market_breakdown": {},
        "direction_breakdown": {},
        "sizes": {
            "min": min(sizes) if sizes else 0,
            "max": max(sizes) if sizes else 0,
            "mean": sum(sizes) / len(sizes) if sizes else 0,
            "at_old_50_cap": at_50_cap,
            "at_500_ceiling": at_new_ceiling,
            "total_fills": len(sizes),
        },
    }

    for strat, spnls in by_strat.items():
        sw = [p for p in spnls if p > 0]
        sl = [p for p in spnls if p <= 0]
        result["strategy_breakdown"][strat] = {
            "trades": len(spnls),
            "wins": len(sw),
            "losses": len(sl),
            "win_rate": len(sw) / len(spnls) if spnls else 0,
            "pnl": sum(spnls),
            "avg_winner": sum(sw) / len(sw) if sw else 0,
            "avg_loser": sum(sl) / len(sl) if sl else 0,
        }

    for mkt, mpnls in by_mkt.items():
        mw = [p for p in mpnls if p > 0]
        ml = [p for p in mpnls if p <= 0]
        result["market_breakdown"][mkt] = {
            "trades": len(mpnls),
            "wins": len(mw),
            "losses": len(ml),
            "win_rate": len(mw) / len(mpnls) if mpnls else 0,
            "pnl": sum(mpnls),
        }

    for d, dpnls in by_dir.items():
        dw = [p for p in dpnls if p > 0]
        result["direction_breakdown"][d] = {
            "trades": len(dpnls),
            "wins": len(dw),
            "win_rate": len(dw) / len(dpnls) if dpnls else 0,
            "pnl": sum(dpnls),
        }

    return result


def compute_signal_metrics(traces):
    """Compute signal pipeline metrics from signal execution trace rows."""
    if not traces:
        return None

    risk = Counter(t.get("risk_decision", "?") for t in traces)
    order = Counter(t.get("order_status", "?") for t in traces)

    # Rejection reasons (simplified)
    reject_reasons = Counter()
    for t in traces:
        if t.get("risk_decision") != "REJECT":
            continue
        reason = t.get("risk_reason", "unknown")
        r = reason.lower()
        if "exposure" in r:
            reject_reasons["exposure_limits"] += 1
        elif "kill switch" in r:
            reject_reasons["kill_switch"] += 1
        elif "drawdown" in r:
            reject_reasons["drawdown"] += 1
        elif "daily loss" in r:
            reject_reasons["daily_loss"] += 1
        elif "cooldown" in r:
            reject_reasons["cooldown"] += 1
        elif "latency" in r:
            reject_reasons["latency"] += 1
        elif "net_ev" in r:
            reject_reasons["net_ev"] += 1
        elif "disagreement" in r:
            reject_reasons["disagreement"] += 1
        elif "volatility" in r:
            reject_reasons["volatility"] += 1
        else:
            reject_reasons["other"] += 1

    # Reduce reasons
    reduce_reasons = Counter()
    for t in traces:
        if t.get("risk_decision") != "REDUCE":
            continue
        reason = t.get("risk_reason", "unknown")
        r = reason.lower()
        if "exposure" in r:
            reduce_reasons["exposure_reduced"] += 1
        elif "disagreement" in r:
            reduce_reasons["disagreement_cap"] += 1
        elif "fragile" in r:
            reduce_reasons["fragile_certainty"] += 1
        else:
            reduce_reasons["other"] += 1

    # Strategy signal counts
    by_strat = Counter(t.get("strategy", "?") for t in traces)

    return {
        "total_signals": len(traces),
        "risk_decisions": dict(risk),
        "order_outcomes": dict(order),
        "rejection_reasons": dict(reject_reasons.most_common(10)),
        "reduce_reasons": dict(reduce_reasons.most_common(5)),
        "by_strategy": dict(by_strat),
        "fill_rate": order.get("FILLED", 0) / len(traces) if traces else 0,
    }


# ---------------------------------------------------------------------------
# Step 7: Format and output
# ---------------------------------------------------------------------------
def format_report(boundary, trade_metrics, signal_metrics, perf_report, fill_count):
    """Build the full text report."""
    lines = []
    w = lines.append

    w("=" * 70)
    w("  PROJECT13 — VPS CURRENT RUN ANALYSIS")
    w(f"  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    w("=" * 70)

    # --- Run boundary ---
    w("")
    w("1. RUN BOUNDARY")
    w("-" * 40)
    w(f"  Detection method:  {boundary['method']}")
    w(f"  Run ID:            {boundary['run_id'] or 'unknown'}")
    w(f"  Start time:        {boundary['start_str'] or 'unknown'}")
    w(f"  Trace file:        {boundary['trace_file'] or 'none'}")
    if boundary["caveats"]:
        for c in boundary["caveats"]:
            w(f"  CAVEAT: {c}")

    # --- Files used ---
    w("")
    w("2. FILES USED")
    w("-" * 40)
    for f in [TRADE_LOG, FILL_TRACE, PERF_LATEST]:
        exists = f.exists()
        size = f.stat().st_size if exists else 0
        w(f"  {'OK' if exists else 'MISSING':>7}  {f.name}  ({size:,} bytes)" if exists else f"  MISSING  {f.name}")
    for f in find_signal_traces()[:3]:
        w(f"      OK  {f.name}  ({f.stat().st_size:,} bytes)")

    # --- Latest perf report ---
    if perf_report:
        w("")
        w("3. LATEST PERFORMANCE REPORT SNAPSHOT")
        w("-" * 40)
        w(f"  Timestamp:     {perf_report.get('timestamp', '?')}")
        w(f"  Session:       {perf_report.get('session_min', '?')} minutes")
        w(f"  Capital:       ${perf_report.get('capital', '?')}")
        w(f"  HWM:           ${perf_report.get('hwm', '?')}")
        w(f"  Trades:        {perf_report.get('total_trades', '?')}")
        w(f"  Win Rate:      {perf_report.get('win_rate', '?')}%")
        w(f"  PnL:           ${perf_report.get('pnl_total', '?')}")
        w(f"  Profit Factor: {perf_report.get('profit_factor', '?')}")
        w(f"  Max Drawdown:  {perf_report.get('max_drawdown', '?')}%")
        w(f"  Sharpe:        {perf_report.get('sharpe', '?')}")
    else:
        w("")
        w("3. LATEST PERFORMANCE REPORT: NOT FOUND")

    # --- Trade metrics ---
    tm = trade_metrics
    w("")
    w("4. CORE PERFORMANCE (from trade_log.jsonl)")
    w("-" * 40)
    if tm["total_resolved"] == 0:
        w("  No resolved trades found in trade log.")
        if tm["total_filled"] > 0:
            w(f"  ({tm['total_filled']} filled but unresolved)")
    else:
        w(f"  Total filled:       {tm['total_filled']}")
        w(f"  Total resolved:     {tm['total_resolved']}")
        w(f"  Unresolved:         {tm['unresolved']}")
        w(f"  Realized PnL:       ${tm['total_pnl']:+,.2f}")
        w(f"  Win rate:           {tm['win_rate']:.1%} ({tm['win_count']}W / {tm['loss_count']}L)")
        pf = tm["profit_factor"]
        w(f"  Profit factor:      {pf:.2f}" if pf != float("inf") else "  Profit factor:      inf (no losses)")
        w(f"  Expectancy:         ${tm['avg_pnl']:+.2f}/trade")
        w(f"  Avg winner:         ${tm['avg_winner']:+.2f}")
        w(f"  Avg loser:          ${tm['avg_loser']:.2f}")
        w(f"  Largest win:        ${tm['largest_win']:+.2f}")
        w(f"  Largest loss:       ${tm['largest_loss']:.2f}")
        w(f"  Gross wins:         ${tm['gross_wins']:,.2f}")
        w(f"  Gross losses:       ${tm['gross_losses']:,.2f}")

    # --- Strategy breakdown ---
    if tm.get("strategy_breakdown"):
        w("")
        w("5. STRATEGY BREAKDOWN")
        w("-" * 40)
        for strat, s in sorted(tm["strategy_breakdown"].items()):
            w(f"  {strat}:")
            w(f"    Trades: {s['trades']}  Wins: {s['wins']}  Losses: {s['losses']}  WR: {s['win_rate']:.1%}")
            w(f"    PnL: ${s['pnl']:+,.2f}  Avg W: ${s['avg_winner']:+.2f}  Avg L: ${s['avg_loser']:.2f}")

    # --- Market breakdown ---
    if tm.get("market_breakdown"):
        w("")
        w("6. MARKET / TIMEFRAME BREAKDOWN")
        w("-" * 40)
        for mkt, m in sorted(tm["market_breakdown"].items()):
            w(f"  {mkt}:")
            w(f"    Trades: {m['trades']}  Wins: {m['wins']}  Losses: {m['losses']}  WR: {m['win_rate']:.1%}")
            w(f"    PnL: ${m['pnl']:+,.2f}")

    # --- Direction breakdown ---
    if tm.get("direction_breakdown"):
        w("")
        w("7. DIRECTION BREAKDOWN")
        w("-" * 40)
        for d, db in sorted(tm["direction_breakdown"].items()):
            w(f"  {d}: {db['trades']} trades, {db['win_rate']:.1%} WR, ${db['pnl']:+,.2f} PnL")

    # --- Position sizing ---
    sz = tm.get("sizes", {})
    if sz.get("total_fills", 0) > 0:
        w("")
        w("8. POSITION SIZING")
        w("-" * 40)
        w(f"  Total fills:               {sz['total_fills']}")
        w(f"  Size min:                  ${sz['min']:.2f}")
        w(f"  Size max:                  ${sz['max']:.2f}")
        w(f"  Size mean:                 ${sz['mean']:.2f}")
        w(f"  At old $50 cap (>=49.50):  {sz['at_old_50_cap']}")
        w(f"  At $500 ceiling (>=499.50):{sz['at_500_ceiling']}")
        if sz["max"] >= 49.5:
            w(f"  ** Old $50 cap WAS binding ({sz['at_old_50_cap']} trades at cap)")
        elif sz["max"] < 49.5:
            w(f"  Old $50 cap was NOT binding (max trade ${sz['max']:.2f})")

    # --- Signal pipeline ---
    if signal_metrics:
        sm = signal_metrics
        w("")
        w("9. SIGNAL PIPELINE & REJECTIONS")
        w("-" * 40)
        w(f"  Total signals:    {sm['total_signals']}")
        for k, v in sm["risk_decisions"].items():
            pct = v / sm["total_signals"] * 100
            w(f"    {k}: {v} ({pct:.1f}%)")
        w(f"  Fill rate:        {sm['fill_rate']:.1%}")
        w("")
        w(f"  Order outcomes:")
        for k, v in sm["order_outcomes"].items():
            w(f"    {k}: {v}")

        if sm["rejection_reasons"]:
            w("")
            w(f"  Top rejection reasons:")
            for reason, count in sm["rejection_reasons"].items():
                w(f"    {count:>5}  {reason}")

        if sm["reduce_reasons"]:
            w("")
            w(f"  Reduction reasons:")
            for reason, count in sm["reduce_reasons"].items():
                w(f"    {count:>5}  {reason}")

        if sm["by_strategy"]:
            w("")
            w(f"  Signals by strategy:")
            for s, c in sm["by_strategy"].items():
                w(f"    {s}: {c}")

    # --- Fill trace concurrent positions ---
    if fill_count is not None:
        w("")
        w("10. CONCURRENT POSITIONS (from fill trace)")
        w("-" * 40)
        if fill_count:
            for k in sorted(fill_count.keys()):
                w(f"  {k} open at fill time: {fill_count[k]} fills")
        else:
            w("  No fill trace data for current run.")

    # --- Caveats ---
    w("")
    w("CAVEATS")
    w("-" * 40)
    caveats = list(boundary.get("caveats", []))
    if not TRADE_LOG.exists():
        caveats.append("trade_log.jsonl not found — trade metrics unavailable.")
    if tm["total_resolved"] == 0 and tm["total_filled"] > 0:
        caveats.append(f"{tm['total_filled']} filled trades have no PnL yet (unresolved).")
    if not find_signal_traces():
        caveats.append("No signal execution trace files found — rejection analysis unavailable.")
    if not caveats:
        caveats.append("None.")
    for c in caveats:
        w(f"  - {c}")

    w("")
    w("=" * 70)
    w("  END OF REPORT")
    w("=" * 70)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"Project13 VPS Current Run Analysis")
    print(f"Working directory: {REPO}")
    print()

    # Step 1: Detect run boundary
    boundary = detect_run_boundary()
    since_ts = boundary["start_ts"]
    run_id = boundary["run_id"]
    print(f"Run boundary: {boundary['method']} → {boundary['start_str'] or 'unknown'}")

    # Step 2: Load trade log
    orders = load_trade_log_deduped(since_ts=since_ts)
    print(f"Trade log: {len(orders)} orders (filtered to current run)")

    # Step 3: Compute trade metrics
    trade_metrics = compute_trade_metrics(orders)

    # Step 4: Load signal traces
    traces = load_signal_traces(run_id=run_id, since_ts=since_ts)
    print(f"Signal traces: {len(traces)} entries")
    signal_metrics = compute_signal_metrics(traces) if traces else None

    # Step 5: Load fill trace
    fills = load_fill_trace(since_ts=since_ts)
    fill_count = Counter(f.get("open_positions_count", 0) for f in fills) if fills else None
    print(f"Fill trace: {len(fills)} entries")

    # Step 6: Parse perf report
    perf_report = parse_perf_report()

    # Step 7: Format and output
    report = format_report(boundary, trade_metrics, signal_metrics, perf_report, fill_count)

    print()
    print(report)

    # Write to file
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report + "\n")
    print(f"\nReport written to: {OUTPUT}")


if __name__ == "__main__":
    main()
