from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv
from utils.logger import get_logger
import config

log = get_logger("config_loader")

REQUIRED_VARS: list[str] = [
    # Add required env vars here as project grows
    # e.g. "POLYMARKET_API_KEY", "PRIVATE_KEY"
]


def load_env(env_path: str | None = None) -> None:
    """Load .env file and validate required variables."""
    path = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        load_dotenv(path)
        log.info(f"Loaded env from {path}")
    else:
        log.warning(f"No .env file found at {path} — using system environment")

    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        log.error(f"Missing required env vars: {missing}")
        raise EnvironmentError(f"Missing required environment variables: {missing}")


def validate_config() -> None:
    """Validate critical config parameters at startup."""
    errors = []

    if config.STALE_THRESHOLD <= 0:
        errors.append(f"STALE_THRESHOLD must be > 0, got {config.STALE_THRESHOLD}")
    if config.ROLLING_WINDOW_SIZE < 20:
        errors.append(f"ROLLING_WINDOW_SIZE must be >= 20, got {config.ROLLING_WINDOW_SIZE}")
    if config.RECONNECT_BASE_DELAY <= 0:
        errors.append(f"RECONNECT_BASE_DELAY must be > 0, got {config.RECONNECT_BASE_DELAY}")
    if config.DASHBOARD_REFRESH_INTERVAL <= 0:
        errors.append(f"DASHBOARD_REFRESH_INTERVAL must be > 0, got {config.DASHBOARD_REFRESH_INTERVAL}")
    if not 0 < config.DAILY_LOSS_LIMIT_PCT <= 1:
        errors.append(f"DAILY_LOSS_LIMIT_PCT must be in (0,1], got {config.DAILY_LOSS_LIMIT_PCT}")
    if not 0 < config.MAX_DRAWDOWN_PCT <= 1:
        errors.append(f"MAX_DRAWDOWN_PCT must be in (0,1], got {config.MAX_DRAWDOWN_PCT}")

    if errors:
        for e in errors:
            log.error(f"Config validation failed: {e}")
        raise ValueError(f"Invalid configuration: {errors}")

    log.info("Config validated successfully")
    log.info(f"  Stale threshold: {config.STALE_THRESHOLD}s")
    log.info(f"  Rolling window:  {config.ROLLING_WINDOW_SIZE} ticks")
    log.info(f"  Reconnect:       {config.RECONNECT_BASE_DELAY}s base, {config.RECONNECT_MAX_DELAY}s max")
