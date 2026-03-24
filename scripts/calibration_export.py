#!/usr/bin/env python3
"""Build calibration dataset + summary from paper trade log and signal traces.

Does not import trading engine code — stdlib + json only.
Run from project root: python3 scripts/calibration_export.py
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_jsonl_by_last_key(
    path: Path,
    key_fn: Callable[[dict[str, Any]], Any],
) -> dict[Any, dict[str, Any]]:
    """Parse JSONL; duplicate keys keep the last row (canonical state)."""
    by_key: dict[Any, dict[str, Any]] = {}
    if not path.exists():
        return by_key
    for line_num, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        k = key_fn(row)
        if k is None or k == "":
            continue
        by_key[k] = row
    return by_key


def dedupe_orders(trade_log_path: Path) -> dict[str, dict[str, Any]]:
    """Dedupe trade log by order_id; last JSONL line per order_id wins."""
    return load_jsonl_by_last_key(
        trade_log_path,
        lambda r: str(r.get("order_id") or "").strip() or None,
    )


def dedupe_signal_traces(trace_path: Path) -> dict[str, dict[str, Any]]:
    """Last row per signal_id (multiple trace lines per signal over time)."""
    return load_jsonl_by_last_key(
        trace_path,
        lambda r: str(r.get("signal_id") or "").strip() or None,
    )


def resolved_filled_orders(orders: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for o in orders.values():
        if o.get("status") != "FILLED":
            continue
        if o.get("pnl") is None:
            continue
        out.append(o)
    return out


def model_prob_bucket(p: Optional[float]) -> str:
    if p is None:
        return "unknown"
    try:
        x = float(p)
    except (TypeError, ValueError):
        return "unknown"
    if x < 0 or x > 1:
        return "out_of_range"
    # 10 deciles [0,0.1), ... [0.9,1.0]
    idx = min(9, int(x * 10))
    lo = idx / 10.0
    hi = (idx + 1) / 10.0
    return f"[{lo:.1f},{hi:.1f})"


def build_rows(
    orders: list[dict[str, Any]],
    traces: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for o in orders:
        sid = str(o.get("signal_id") or "").strip()
        meta = o.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        tr = traces.get(sid) if sid else {}
        if not isinstance(tr, dict):
            tr = {}

        row = {
            "signal_id": sid,
            "order_id": str(o.get("order_id") or ""),
            "timestamp": o.get("timestamp"),
            "fill_timestamp": o.get("fill_timestamp"),
            "market_type": o.get("market_type") or "",
            "direction": o.get("direction") or "",
            "strategy": meta.get("strategy", ""),
            "model_probability": meta.get("model_probability"),
            "market_probability": meta.get("market_probability"),
            "edge": meta.get("edge"),
            "net_ev": tr.get("net_ev"),
            "kelly_size": tr.get("kelly_size"),
            "fill_price": o.get("fill_price"),
            "size_usdc": o.get("size_usdc"),
            "num_shares": o.get("num_shares"),
            "pnl": o.get("pnl"),
            "condition_id": meta.get("condition_id", ""),
        }
        rows.append(row)
    return rows


CSV_FIELDS = [
    "signal_id",
    "order_id",
    "timestamp",
    "fill_timestamp",
    "market_type",
    "direction",
    "strategy",
    "model_probability",
    "market_probability",
    "edge",
    "net_ev",
    "kelly_size",
    "fill_price",
    "size_usdc",
    "num_shares",
    "pnl",
    "condition_id",
]


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CSV_FIELDS})


def write_summary(rows: list[dict[str, Any]], path: Path) -> str:
    n = len(rows)
    if n == 0:
        text = (
            "Calibration summary\n"
            "====================\n"
            "Resolved trades: 0\n"
            "No FILLED orders with pnl in trade log.\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return text

    total_pnl = sum(float(r["pnl"]) for r in rows if r.get("pnl") is not None)
    wins = sum(1 for r in rows if r.get("pnl") is not None and float(r["pnl"]) > 0)
    win_rate = wins / n

    lines = [
        "Calibration summary",
        "====================",
        f"Resolved trades: {n}",
        f"Total PnL (USDC): {total_pnl:.4f}",
        f"Win rate: {win_rate:.4f} ({wins}/{n})",
        "",
        "By strategy",
        "-----------",
    ]

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        s = str(r.get("strategy") or "(empty)")
        by_strat[s].append(r)

    for s in sorted(by_strat.keys()):
        grp = by_strat[s]
        pnls = [float(x["pnl"]) for x in grp if x.get("pnl") is not None]
        wn = sum(1 for p in pnls if p > 0)
        lines.append(
            f"  {s}: n={len(grp)}  total_pnl={sum(pnls):.4f}  win_rate={wn/len(grp):.4f}"
        )

    lines.extend(["", "By market_type", "--------------"])
    by_mt: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        m = str(r.get("market_type") or "(empty)")
        by_mt[m].append(r)
    for m in sorted(by_mt.keys()):
        grp = by_mt[m]
        pnls = [float(x["pnl"]) for x in grp if x.get("pnl") is not None]
        wn = sum(1 for p in pnls if p > 0)
        lines.append(
            f"  {m}: n={len(grp)}  total_pnl={sum(pnls):.4f}  win_rate={wn/len(grp):.4f}"
        )

    lines.extend(["", "By model_probability bucket", "-----------------------------"])
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        b = model_prob_bucket(
            float(r["model_probability"])
            if r.get("model_probability") is not None
            else None
        )
        by_b[b].append(r)
    for b in sorted(by_b.keys()):
        grp = by_b[b]
        pnls = [float(x["pnl"]) for x in grp if x.get("pnl") is not None]
        wn = sum(1 for p in pnls if p > 0)
        lines.append(
            f"  {b}: n={len(grp)}  total_pnl={sum(pnls):.4f}  win_rate={wn/len(grp):.4f}"
        )

    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text


def main() -> None:
    root = _project_root()
    p = argparse.ArgumentParser(description="Calibration CSV + summary from paper logs")
    p.add_argument(
        "--trade-log",
        type=Path,
        default=root / "logs" / "trade_log.jsonl",
        help="Path to trade_log.jsonl",
    )
    p.add_argument(
        "--signal-trace",
        type=Path,
        default=root / "logs" / "signal_execution_trace.jsonl",
        help="Path to signal_execution_trace.jsonl",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=root / "data",
        help="Directory for calibration_resolved_trades.csv and calibration_summary.txt",
    )
    args = p.parse_args()

    orders_map = dedupe_orders(args.trade_log)
    resolved = resolved_filled_orders(orders_map)
    traces = dedupe_signal_traces(args.signal_trace)
    rows = build_rows(resolved, traces)

    out_dir = args.output_dir
    csv_path = out_dir / "calibration_resolved_trades.csv"
    summary_path = out_dir / "calibration_summary.txt"

    write_csv(rows, csv_path)
    summary = write_summary(rows, summary_path)

    print(summary)
    print(f"Wrote: {csv_path}")
    print(f"Wrote: {summary_path}")


if __name__ == "__main__":
    main()
