#!/bin/bash
# Project13 — Startup script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "=== Project13 Startup ==="

# Check virtual environment (.venv first, then venv fallback)
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo "[OK] Virtual environment activated: .venv ($VIRTUAL_ENV)"
elif [ -d "venv" ]; then
    source venv/bin/activate
    echo "[OK] Virtual environment activated: venv ($VIRTUAL_ENV)"
elif [ -n "$VIRTUAL_ENV" ]; then
    echo "[OK] Already in virtual environment: $VIRTUAL_ENV"
else
    echo "[WARN] No virtual environment found — using system Python"
fi

# Check .env
if [ ! -f ".env" ]; then
    echo "[ERROR] .env file not found. Copy .env.example to .env and configure."
    exit 1
fi
echo "[OK] .env found"

# Ensure logs directory
mkdir -p logs
echo "[OK] Logs directory ready"

# Validate imports
python -c "from main import main; print('[OK] Imports validated')" || {
    echo "[ERROR] Import validation failed"
    exit 1
}

# Check for existing process
if pgrep -f "python.*main.py" > /dev/null 2>&1; then
    echo "[WARN] Bot appears to be already running (PID: $(pgrep -f 'python.*main.py'))"
    echo "       Use deploy/stop.sh first if you want to restart."
    exit 1
fi

# Start bot
echo "[START] Launching Project13..."
nohup python main.py >> logs/bot.log 2>&1 &
BOT_PID=$!
echo "[OK] Bot started with PID $BOT_PID"
echo "$BOT_PID" > logs/bot.pid
echo "     Logs: tail -f logs/bot.log"
echo "     Stop: ./deploy/stop.sh"
