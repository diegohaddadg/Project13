# Project13 — Deployment Guide

## Recommended VPS Setup

- **OS:** Ubuntu 22.04+ or Debian 12+
- **CPU:** 1-2 vCPU
- **RAM:** 1 GB minimum, 2 GB recommended
- **Disk:** 10 GB SSD
- **Network:** Low-latency connection (US East preferred for Polymarket/Binance proximity)

## Python Requirements

- Python 3.9.10+ (3.11+ recommended)
- pip / venv

## Setup

```bash
# Clone repo
git clone <repo-url> Project13
cd Project13

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Ensure logs directory exists
mkdir -p logs
```

## Running in Paper Mode (Default)

```bash
# Interactive
python main.py

# Background (production)
./deploy/startup.sh
```

## Validating Paper Results

Before considering live mode:

1. Run paper trading for at least 24 hours
2. Review `logs/trade_log.jsonl` for trade quality
3. Check `logs/performance_report.txt` for overall performance
4. Verify:
   - Win rate is reasonable (>40%)
   - Profit factor > 1.0
   - Max drawdown within acceptable range
   - No kill switch triggers from system issues
   - Feed health is consistently good

## Switching to Live Mode

**Do NOT switch to live until paper results are validated.**

1. Ensure Python >=3.9.10 and install `py-clob-client`:
   ```bash
   pip install py-clob-client eth-account web3
   ```

2. Set up Polymarket wallet credentials in `.env`:
   ```
   POLYMARKET_PRIVATE_KEY=<your_polygon_private_key>
   ```

3. Derive API credentials:
   ```python
   from utils.polymarket_auth import get_clob_client
   client = get_clob_client(authenticated=True)
   ```

4. Switch execution mode:
   ```
   EXECUTION_MODE=live
   TRADING_ENABLED=true
   LIVE_TRADING_CONFIRMATION=I_UNDERSTAND
   STARTING_CAPITAL_USDC=<your_actual_capital>
   ```

5. Start with conservative limits:
   ```
   MAX_DRAWDOWN_PCT=0.10
   DAILY_LOSS_LIMIT_PCT=0.10
   ```

## Monitoring Logs Remotely

```bash
# Watch live output
tail -f logs/bot.log

# Check trade history
cat logs/trade_log.jsonl | python3 -m json.tool --no-ensure-ascii

# Check performance
cat logs/performance_report.txt
```

## Emergency Stop

```bash
# Graceful
./deploy/stop.sh

# Force (if graceful fails)
pkill -9 -f "python.*main.py"

# Via config (prevents restart from trading)
# Set in .env: KILL_SWITCH_ACTIVE=true
```

## Health Checks

```bash
# One-time diagnostic
python health_check.py --duration 60

# Quick import test
python -c "from main import main; print('OK')"
```
