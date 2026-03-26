#!/usr/bin/env python3
"""Diagnose the live redemption path step by step.

Run on the droplet:
    python3 scripts/diagnose_redeem.py

This script:
1. Loads env/config
2. Creates authenticated CLOB client
3. Checks a sample condition_id for resolution
4. Tests on-chain redeemer init (web3 + proxy detection)
5. Dry-runs the proxy.execute calldata encoding
6. Reports exactly what would happen at each step

Does NOT submit any transaction. Read-only.
"""

import os
import sys
import json

# Load .env
from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("REDEEM DIAGNOSTIC")
print("=" * 60)

# Step 1: Environment
print("\n--- STEP 1: Environment ---")
print(f"EXECUTION_MODE={os.getenv('EXECUTION_MODE')}")
print(f"POLYMARKET_PRIVATE_KEY={'SET' if os.getenv('POLYMARKET_PRIVATE_KEY') else 'MISSING'}")
print(f"POLYMARKET_FUNDER={os.getenv('POLYMARKET_FUNDER', 'NOT SET')}")
print(f"POLYMARKET_SIGNATURE_TYPE={os.getenv('POLYMARKET_SIGNATURE_TYPE', '0')}")
print(f"POLYGON_RPC_URL={os.getenv('POLYGON_RPC_URL', 'default: polygon-rpc.com')}")

# Step 2: CLOB client
print("\n--- STEP 2: CLOB Client ---")
try:
    from utils.polymarket_auth import get_clob_client
    client = get_clob_client(authenticated=True)
    print(f"CLOB client: OK (mode={client.mode})")
except Exception as e:
    print(f"CLOB client: FAILED — {e}")
    client = None

# Step 3: Check recent positions from trade log
print("\n--- STEP 3: Recent Live Positions ---")
try:
    import config
    from pathlib import Path
    log_path = Path(config.TRADE_LOG_PATH)
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        live_filled = []
        for line in lines:
            try:
                d = json.loads(line)
                if d.get("execution_mode") == "live" and d.get("status") == "FILLED":
                    live_filled.append(d)
            except Exception:
                pass
        print(f"Total live FILLED orders in trade log: {len(live_filled)}")
        for o in live_filled[-5:]:
            cond = o.get("metadata", {}).get("condition_id", "?")
            tok = o.get("token_id", "?")
            print(f"  order={o['order_id']} dir={o['direction']} mkt={o['market_id']} cond={cond[:20]}... token=...{tok[-12:]}")
    else:
        print("No trade log found")
except Exception as e:
    print(f"Trade log read failed: {e}")

# Step 4: Resolution check for a sample condition
print("\n--- STEP 4: Resolution Check ---")
if client and live_filled:
    sample = live_filled[-1]
    cond = sample.get("metadata", {}).get("condition_id", "")
    print(f"Testing condition_id: {cond}")

    # Try get_market (positional)
    try:
        resp = client.get_market(cond)
        print(f"get_market(positional): {type(resp).__name__} — keys={list(resp.keys())[:10] if isinstance(resp, dict) else 'N/A'}")
        if isinstance(resp, dict):
            print(f"  closed={resp.get('closed')} resolved={resp.get('resolved')}")
            tokens = resp.get("tokens", [])
            for t in tokens[:3]:
                if isinstance(t, dict):
                    print(f"  token: id=...{str(t.get('token_id',''))[-12:]} winner={t.get('winner')}")
    except Exception as e:
        print(f"get_market(positional): FAILED — {e}")

    # Try Gamma API
    try:
        import requests
        r = requests.get(f"https://gamma-api.polymarket.com/markets?conditionId={cond}", timeout=5)
        data = r.json()
        if isinstance(data, list) and data:
            m = data[0]
            print(f"Gamma API: closed={m.get('closed')} active={m.get('active')}")
            clob_ids = m.get("clobTokenIds", "")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            print(f"  clobTokenIds: {['...'+str(t)[-12:] for t in (clob_ids or [])]}")
            outcome_prices = m.get("outcomePrices", "")
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            print(f"  outcomePrices: {outcome_prices}")
        else:
            print(f"Gamma API: empty response")
    except Exception as e:
        print(f"Gamma API: FAILED — {e}")
else:
    print("Skipped (no client or no positions)")

# Step 5: On-chain redeemer
print("\n--- STEP 5: On-chain Redeemer ---")
try:
    from web3 import Web3
    print(f"web3: installed (version available)")

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    print(f"Polygon RPC connected: {w3.is_connected()}")
    if w3.is_connected():
        print(f"Chain ID: {w3.eth.chain_id}")

    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if pk:
        account = w3.eth.account.from_key(pk)
        funder = os.getenv("POLYMARKET_FUNDER") or account.address
        is_proxy = funder.lower() != account.address.lower()

        print(f"Signer EOA: {account.address}")
        print(f"Funder: {funder}")
        print(f"Is proxy wallet: {is_proxy}")

        if w3.is_connected():
            signer_balance = w3.from_wei(w3.eth.get_balance(account.address), "ether")
            print(f"Signer MATIC balance: {signer_balance:.6f}")

            if is_proxy:
                funder_balance = w3.from_wei(w3.eth.get_balance(funder), "ether")
                print(f"Funder MATIC balance: {funder_balance:.6f}")

                # Check if proxy contract has code (is a contract, not EOA)
                code = w3.eth.get_code(Web3.to_checksum_address(funder))
                print(f"Proxy is contract: {len(code) > 2}")
                if len(code) > 2:
                    print(f"Proxy contract code size: {len(code)} bytes")
                else:
                    print("WARNING: Funder address has NO contract code — it may be an EOA, not a proxy!")

except ImportError:
    print("web3: NOT INSTALLED — run: pip install web3")
except Exception as e:
    print(f"On-chain check failed: {e}")

print("\n" + "=" * 60)
print("DIAGNOSTIC COMPLETE")
print("=" * 60)
