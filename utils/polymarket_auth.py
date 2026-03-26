"""Polymarket authentication helper.

Handles credential loading and client initialization for Polymarket CLOB API.
Market data endpoints do NOT require authentication — only trading does.
This module prepares auth for Phase 4 (execution) while providing an
unauthenticated client for Phase 2 (market data).
"""

from __future__ import annotations

import os
from typing import Optional

from utils.logger import get_logger

log = get_logger("polymarket_auth")

# CLOB host
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon


def get_clob_client(authenticated: bool = False):
    """Create a Polymarket CLOB client.

    Args:
        authenticated: If True, initialize with wallet credentials for trading.
                      If False (default), create read-only client for market data.

    Returns:
        ClobClient instance.

    Raises:
        ImportError: If py-clob-client is not installed.
        EnvironmentError: If authenticated=True and required credentials are missing.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        log.error("py-clob-client not installed. Run: pip install py-clob-client")
        raise

    if not authenticated:
        log.info("Creating unauthenticated CLOB client (market data only)")
        return ClobClient(CLOB_HOST)

    # Authenticated client for trading (Phase 4)
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        log.error("POLYMARKET_PRIVATE_KEY not set — cannot create authenticated client")
        raise EnvironmentError(
            "POLYMARKET_PRIVATE_KEY is required for authenticated Polymarket access. "
            "Set it in .env"
        )

    funder = os.getenv("POLYMARKET_FUNDER")
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))  # 0=EOA

    log.info("Creating authenticated CLOB client")
    # Never log the private key
    kwargs = dict(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)
    if funder:
        kwargs["funder"] = funder
        kwargs["signature_type"] = sig_type
        log.info(f"Using funder address: {funder[:10]}...{funder[-6:]}")

    client = ClobClient(**kwargs)

    # Derive or load API credentials (L2 auth)
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_PASSPHRASE")

    if api_key and api_secret and api_passphrase:
        from py_clob_client.clob_types import ApiCreds
        client.set_api_creds(ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ))
        log.info("Loaded API credentials from environment")
    else:
        log.info("Deriving API credentials from private key...")
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            log.info("API credentials derived successfully")
        except Exception as e:
            log.error(f"Failed to derive API credentials: {e}")
            raise

    return client


def validate_live_credentials() -> tuple[bool, list[str]]:
    """Check if all required live trading credentials are present.

    Returns:
        (all_present, list_of_missing_var_names)
    """
    required = ["POLYMARKET_PRIVATE_KEY"]
    # API creds can be derived, but if any are set, all three must be
    api_vars = ["POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_PASSPHRASE"]
    api_set = [v for v in api_vars if os.getenv(v)]

    missing = [v for v in required if not os.getenv(v)]

    # If some but not all API creds are set, flag the missing ones
    if 0 < len(api_set) < 3:
        for v in api_vars:
            if not os.getenv(v):
                missing.append(v)

    return (len(missing) == 0, missing)
