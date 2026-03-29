#!/usr/bin/env bash
# reset_paper_session.sh — Back up old state and start a fresh $100 paper session.
#
# Usage:
#   ./scripts/reset_paper_session.sh
#
# What it does:
#   1. Stops the bot if running (via deploy/stop.sh)
#   2. Creates a timestamped backup of all logs/ and data/ files
#   3. Removes the trade log (the file that drives capital/position restore)
#   4. Removes session-level trace files (competition, fills, dashboard, etc.)
#   5. Preserves design docs and reports that are not session state
#
# After running, start the bot normally (python main.py or deploy/startup.sh).
# It will initialize with a fresh $100 paper balance and zero positions.

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_DIR="${PROJECT_DIR}/logs/backup_${TIMESTAMP}"

echo "=== PAPER SESSION RESET ==="
echo "Project dir: ${PROJECT_DIR}"
echo "Backup dir:  ${BACKUP_DIR}"
echo ""

# 1. Stop bot if running
if [ -f deploy/stop.sh ]; then
    echo "Stopping bot..."
    bash deploy/stop.sh 2>/dev/null || true
    sleep 1
fi

# 2. Create backup directory
mkdir -p "${BACKUP_DIR}"
echo "Created backup: ${BACKUP_DIR}"

# 3. Back up all session state files
SESSION_FILES=(
    "logs/trade_log.jsonl"
    "logs/fill_to_position_trace.jsonl"
    "logs/strategy_competition_trace.jsonl"
    "logs/dashboard_truth_trace.jsonl"
    "logs/dashboard_truth_report.txt"
    "logs/market_timing_truth_report.txt"
    "logs/live_reconciliation.jsonl"
    "logs/missed_windows.jsonl"
    "logs/execution_consistency_audit.txt"
    "data/live_tape.jsonl"
)

backed_up=0
for f in "${SESSION_FILES[@]}"; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        cp "${PROJECT_DIR}/${f}" "${BACKUP_DIR}/$(basename "${f}")"
        echo "  Backed up: ${f}"
        backed_up=$((backed_up + 1))
    fi
done

# Also back up any performance report files
for f in logs/performance_report_*.txt; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        cp "${PROJECT_DIR}/${f}" "${BACKUP_DIR}/$(basename "${f}")"
        backed_up=$((backed_up + 1))
    fi
done

echo "  Total files backed up: ${backed_up}"
echo ""

# 4. Remove session state files (these get recreated on startup)
RESET_FILES=(
    "logs/trade_log.jsonl"
    "logs/fill_to_position_trace.jsonl"
    "logs/strategy_competition_trace.jsonl"
    "logs/dashboard_truth_trace.jsonl"
    "logs/dashboard_truth_report.txt"
    "logs/market_timing_truth_report.txt"
    "logs/live_reconciliation.jsonl"
    "logs/missed_windows.jsonl"
    "logs/execution_consistency_audit.txt"
    "data/live_tape.jsonl"
)

reset_count=0
for f in "${RESET_FILES[@]}"; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        rm "${PROJECT_DIR}/${f}"
        echo "  Reset: ${f}"
        reset_count=$((reset_count + 1))
    fi
done

# Remove timestamped performance reports (backed up already)
for f in logs/performance_report_*.txt; do
    if [ -f "${PROJECT_DIR}/${f}" ]; then
        rm "${PROJECT_DIR}/${f}"
        reset_count=$((reset_count + 1))
    fi
done

echo "  Total files reset: ${reset_count}"
echo ""

echo "=== RESET COMPLETE ==="
echo "Backup location: ${BACKUP_DIR}"
echo "Starting equity: \$100.00 (config.STARTING_CAPITAL_USDC)"
echo ""
echo "Start the bot with:"
echo "  python main.py"
echo "  # or: ./deploy/startup.sh"
