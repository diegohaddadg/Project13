#!/bin/bash
# Project13 — Graceful stop script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "=== Project13 Shutdown ==="

PID_FILE="logs/bot.pid"

if [ -f "$PID_FILE" ]; then
    BOT_PID=$(cat "$PID_FILE")
    if kill -0 "$BOT_PID" 2>/dev/null; then
        echo "[STOP] Sending SIGTERM to PID $BOT_PID..."
        kill "$BOT_PID"

        # Wait for graceful shutdown (up to 10 seconds)
        for i in $(seq 1 10); do
            if ! kill -0 "$BOT_PID" 2>/dev/null; then
                echo "[OK] Bot stopped gracefully"
                rm -f "$PID_FILE"
                exit 0
            fi
            sleep 1
        done

        echo "[WARN] Bot did not stop gracefully. Sending SIGKILL..."
        kill -9 "$BOT_PID" 2>/dev/null || true
        echo "[OK] Bot force-stopped"
        rm -f "$PID_FILE"
    else
        echo "[INFO] PID $BOT_PID is not running"
        rm -f "$PID_FILE"
    fi
else
    # Try to find by process name
    BOT_PID=$(pgrep -f "python.*main.py" 2>/dev/null || true)
    if [ -n "$BOT_PID" ]; then
        echo "[STOP] Found bot at PID $BOT_PID (no PID file)"
        kill "$BOT_PID"
        sleep 2
        if ! kill -0 "$BOT_PID" 2>/dev/null; then
            echo "[OK] Bot stopped"
        else
            kill -9 "$BOT_PID" 2>/dev/null || true
            echo "[OK] Bot force-stopped"
        fi
    else
        echo "[INFO] No running bot found"
    fi
fi
