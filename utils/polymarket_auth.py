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

    # Authenticated client for trading
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        log.error("POLYMARKET_PRIVATE_KEY not set — cannot create authenticated client")
        raise EnvironmentError(
            "POLYMARKET_PRIVATE_KEY is required for authenticated Polymarket access. "
            "Set it in .env"
        )

    funder = os.getenv("POLYMARKET_FUNDER")
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

    # --- Build API creds if available ---
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_PASSPHRASE")
    have_api_creds = bool(api_key and api_secret and api_passphrase)

    creds = None
    if have_api_creds:
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    # --- Construct ClobClient with ALL parameters in one call ---
    # This ensures the client is in the correct mode (L2 if creds provided)
    # from the very first moment, and the OrderBuilder gets the right
    # signature_type and funder from the start.
    kwargs = dict(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=sig_type,
    )
    if funder:
        kwargs["funder"] = funder
    if creds:
        kwargs["creds"] = creds

    # --- Startup logging (never log secrets) ---
    log.info(f"Creating authenticated CLOB client: "
             f"chain_id={CHAIN_ID}, signature_type={sig_type}")
    if funder:
        log.info(f"  funder={funder[:10]}...{funder[-6:]}")
    else:
        log.info("  funder=None (using signer address as maker)")
    log.info(f"  api_creds={'FROM_ENV' if have_api_creds else 'WILL_DERIVE'}")

    client = ClobClient(**kwargs)

    # If no API creds from env, derive them
    if not have_api_creds:
        log.info("Deriving API credentials from private key...")
        try:
            derived_creds = client.create_or_derive_api_creds()
            client.set_api_creds(derived_creds)
            log.info("API credentials derived successfully")
        except Exception as e:
            log.error(f"Failed to derive API credentials: {e}")
            raise

    # --- Post-init verification logging ---
    _log_client_state(client)

    return client


def _log_client_state(client):
    """Log the resolved client state for debugging auth issues."""
    try:
        mode = getattr(client, 'mode', 'unknown')
        signer = getattr(client, 'signer', None)
        signer_addr = signer.address() if signer and hasattr(signer, 'address') else 'unknown'
        builder = getattr(client, 'order_builder', None)
        builder_funder = getattr(builder, 'funder', 'unknown') if builder else 'unknown'
        builder_sig_type = getattr(builder, 'sig_type', 'unknown') if builder else 'unknown'
        has_creds = getattr(client, 'creds', None) is not None

        log.info(f"  client_mode={mode} (0=L0, 1=L1, 2=L2)")
        log.info(f"  signer_address={str(signer_addr)[:10]}...{str(signer_addr)[-6:]}")
        log.info(f"  builder_funder={str(builder_funder)[:10]}...{str(builder_funder)[-6:]}")
        log.info(f"  builder_sig_type={builder_sig_type}")
        log.info(f"  creds_attached={has_creds}")
    except Exception as e:
        log.warning(f"  client state logging failed: {e}")


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
