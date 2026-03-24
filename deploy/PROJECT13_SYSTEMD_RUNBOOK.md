# Project13 — systemd Runbook

## One-Time Install

```bash
# Stop any running instance first
pkill -f "python.*main.py" 2>/dev/null; sleep 2

# Copy service file
cp /root/Project13/deploy/project13.systemd.service /etc/systemd/system/project13.service

# Reload systemd and enable on boot
systemctl daemon-reload
systemctl enable project13
```

## Start / Stop / Restart

```bash
systemctl start project13       # Start the bot
systemctl stop project13        # Graceful stop (SIGTERM → 15s → SIGKILL)
systemctl restart project13     # Stop + start
```

## Status

```bash
systemctl status project13
```

Shows: active/inactive, PID, uptime, last few log lines, restart count.

## Logs

```bash
# Live tail
journalctl -u project13 -f

# Last 100 lines
journalctl -u project13 -n 100

# Since last boot
journalctl -u project13 -b

# Last hour
journalctl -u project13 --since "1 hour ago"

# Errors only
journalctl -u project13 -p err
```

## Disable tmux-Based Startup

If you previously ran the bot inside tmux or via `deploy/startup.sh`:

```bash
# Kill any tmux-managed instances
tmux kill-session -t project13 2>/dev/null
pkill -f "python.*main.py" 2>/dev/null

# Remove any crontab entries that launch tmux/startup.sh
crontab -e   # delete any Project13 lines

# From now on, use only:
systemctl start project13
```

The old `deploy/startup.sh` and `deploy/stop.sh` scripts are still in the repo for local dev use but should not be used on the VPS once systemd is active.

## Auto-Restart Behavior

- **On crash** (non-zero exit): restarts after 5 seconds
- **On clean stop** (`systemctl stop`): stays stopped
- **Crash loop protection**: max 5 restarts in 5 minutes, then gives up
- **To reset after crash loop**: `systemctl reset-failed project13 && systemctl start project13`

## After Deploying New Code

```bash
cd /root/Project13
git pull
systemctl restart project13
journalctl -u project13 -f    # verify it starts cleanly
```

## Updating the Service File

If you edit `deploy/project13.systemd.service`:

```bash
cp /root/Project13/deploy/project13.systemd.service /etc/systemd/system/project13.service
systemctl daemon-reload
systemctl restart project13
```

## Emergency Stop

```bash
# Normal stop
systemctl stop project13

# If that hangs
systemctl kill -s SIGKILL project13

# Nuclear option
pkill -9 -f "python.*main.py"

# Prevent restart until manual intervention
systemctl disable project13
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Won't start | `journalctl -u project13 -n 50` — look for import errors or missing .env |
| Keeps restarting | `systemctl status project13` — check exit code; `journalctl -u project13 --since "10 min ago"` |
| "Start limit hit" | `systemctl reset-failed project13 && systemctl start project13` |
| Dashboard unreachable | Verify port 3000 is open: `ss -tlnp | grep 3000` |
| Feeds not connecting | Check .env for `BINANCE_WS_URL`; run `python3 scripts/test_binance_ws.py` |
