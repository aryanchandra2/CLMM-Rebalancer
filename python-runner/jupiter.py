#!/usr/bin/env python3
"""
Jupiter Swap Module

Handles token swaps via Jupiter Aggregator API.
Used to rebalance inventory after withdrawing liquidity.
"""

import os
import json
import base64
import requests
import base58
from pathlib import Path
from typing import TypedDict
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.rpc.requests import SendRawTransaction
from solders.rpc.responses import SendTransactionResp
from solders.commitment_config import CommitmentLevel

# Load .env from project root
ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)

# Constants
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"


def get_jupiter_api_key() -> str | None:
    """Get Jupiter API key from environment (optional but recommended)."""
    return os.getenv("JUPITER_API_KEY")

# Token mints
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Default constraints
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%
MAX_SLIPPAGE_BPS = 100  # 1% hard cap
MAX_PRICE_IMPACT_PCT = 1.0  # 1% max price impact


class QuoteResponse(TypedDict):
    inputMint: str
    inAmount: str
    outputMint: str
    outAmount: str
    otherAmountThreshold: str
    swapMode: str
    slippageBps: int
    priceImpactPct: str
    routePlan: list


class SwapResult(TypedDict):
    success: bool
    inputMint: str
    outputMint: str
    inAmount: str
    outAmount: str
    txid: str | None
    error: str | None


def load_keypair() -> Keypair:
    """Load keypair from environment variable."""
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    if not private_key:
        raise ValueError("SOLANA_PRIVATE_KEY not set in .env")

    # Handle both base58 and JSON array formats
    try:
        # Try base58 first (Phantom export format)
        key_bytes = base58.b58decode(private_key)
        return Keypair.from_bytes(key_bytes)
    except Exception:
        # Try JSON array format
        try:
            key_array = json.loads(private_key)
            return Keypair.from_bytes(bytes(key_array))
        except Exception:
            raise ValueError("Invalid SOLANA_PRIVATE_KEY format. Use base58 or JSON array.")


def get_rpc_url() -> str:
    """Get RPC URL from environment."""
    rpc_url = os.getenv("SOLANA_RPC_URL")
    if not rpc_url:
        raise ValueError("RPC_URL not set in .env")
    return rpc_url


def get_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> QuoteResponse:
    """
    Get a swap quote from Jupiter.

    Args:
        input_mint: Input token mint address
        output_mint: Output token mint address
        amount: Amount in smallest units (lamports for SOL, etc.)
        slippage_bps: Slippage tolerance in basis points

    Returns:
        Quote response from Jupiter API
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(min(slippage_bps, MAX_SLIPPAGE_BPS)),
        "restrictIntermediateTokens": "true",
    }

    headers = {}
    api_key = get_jupiter_api_key()
    if api_key:
        headers["x-api-key"] = api_key

    response = requests.get(JUPITER_QUOTE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()

    quote = response.json()

    # Check for API error
    if "error" in quote:
        raise ValueError(f"Jupiter quote error: {quote['error']}")

    # Validate price impact
    price_impact = float(quote.get("priceImpactPct", "0"))
    if price_impact > MAX_PRICE_IMPACT_PCT:
        raise ValueError(
            f"Price impact too high: {price_impact:.2f}% > {MAX_PRICE_IMPACT_PCT}%"
        )

    return quote


def build_swap_transaction(
    quote: QuoteResponse,
    user_pubkey: str,
) -> str:
    """
    Build a swap transaction from a quote.

    Args:
        quote: Quote response from get_quote()
        user_pubkey: User's public key as string

    Returns:
        Base64 encoded serialized transaction
    """
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "dynamicComputeUnitLimit": True,
        "dynamicSlippage": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": 1000000,  # 0.001 SOL max priority fee
                "priorityLevel": "veryHigh"
            }
        }
    }

    headers = {"Content-Type": "application/json"}
    api_key = get_jupiter_api_key()
    if api_key:
        headers["x-api-key"] = api_key

    response = requests.post(
        JUPITER_SWAP_URL,
        json=payload,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    swap_response = response.json()

    if "error" in swap_response:
        raise ValueError(f"Jupiter swap error: {swap_response['error']}")

    if swap_response.get("simulationError"):
        raise ValueError(f"Simulation failed: {swap_response['simulationError']}")

    return swap_response["swapTransaction"]


def send_transaction(
    transaction_base64: str,
    keypair: Keypair,
    rpc_url: str,
) -> str:
    """
    Sign and send a transaction.

    Args:
        transaction_base64: Base64 encoded serialized transaction
        keypair: Keypair to sign with
        rpc_url: Solana RPC URL

    Returns:
        Transaction signature
    """
    # Deserialize transaction
    tx_bytes = base64.b64decode(transaction_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    # Use solders' native signing - this handles versioned message signing correctly
    signed_tx = VersionedTransaction(tx.message, [keypair])

    # Serialize for sending
    serialized = bytes(signed_tx)

    # Send via RPC
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(serialized).decode("utf-8"),
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "maxRetries": 3,
                }
            ]
        },
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()

    result = response.json()

    if "error" in result:
        raise ValueError(f"RPC error: {result['error']}")

    return result["result"]


def confirm_transaction(txid: str, rpc_url: str, timeout_seconds: int = 60) -> bool:
    """
    Wait for transaction confirmation.

    Args:
        txid: Transaction signature
        rpc_url: Solana RPC URL
        timeout_seconds: Max time to wait

    Returns:
        True if confirmed, raises exception otherwise
    """
    import time

    start = time.time()
    while time.time() - start < timeout_seconds:
        response = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[txid], {"searchTransactionHistory": True}]
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()

        if "error" in result:
            raise ValueError(f"RPC error: {result['error']}")

        statuses = result.get("result", {}).get("value", [])
        if statuses and statuses[0]:
            status = statuses[0]
            if status.get("err"):
                raise ValueError(f"Transaction failed: {status['err']}")
            if status.get("confirmationStatus") in ["confirmed", "finalized"]:
                return True

        time.sleep(2)

    raise TimeoutError(f"Transaction not confirmed within {timeout_seconds}s")


def swap(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> SwapResult:
    """
    Execute a token swap via Jupiter.

    Args:
        input_mint: Input token mint address
        output_mint: Output token mint address
        amount: Amount in smallest units
        slippage_bps: Slippage tolerance in basis points

    Returns:
        SwapResult with transaction details
    """
    txid = None
    quote = None

    try:
        # Load credentials
        keypair = load_keypair()
        rpc_url = get_rpc_url()
        user_pubkey = str(keypair.pubkey())

        # Get quote
        quote = get_quote(input_mint, output_mint, amount, slippage_bps)

        # Build transaction
        tx_base64 = build_swap_transaction(quote, user_pubkey)

        # Send transaction
        txid = send_transaction(tx_base64, keypair, rpc_url)

        # Confirm transaction
        confirm_transaction(txid, rpc_url)

        return {
            "success": True,
            "inputMint": input_mint,
            "outputMint": output_mint,
            "inAmount": quote["inAmount"],
            "outAmount": quote["outAmount"],
            "txid": txid,
            "error": None,
        }

    except Exception as e:
        return {
            "success": False,
            "inputMint": input_mint,
            "outputMint": output_mint,
            "inAmount": quote["inAmount"] if quote else str(amount),
            "outAmount": quote["outAmount"] if quote else "0",
            "txid": txid,  # Include txid even on failure so user can check
            "error": str(e),
        }


def swap_sol_to_usdc(amount_lamports: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> SwapResult:
    """Convenience function to swap SOL to USDC."""
    return swap(SOL_MINT, USDC_MINT, amount_lamports, slippage_bps)


def swap_usdc_to_sol(amount_usdc: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> SwapResult:
    """Convenience function to swap USDC to SOL."""
    return swap(USDC_MINT, SOL_MINT, amount_usdc, slippage_bps)


# CLI interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Jupiter swap CLI")
    parser.add_argument("--input-mint", required=True, help="Input token mint")
    parser.add_argument("--output-mint", required=True, help="Output token mint")
    parser.add_argument("--amount", required=True, type=int, help="Amount in smallest units")
    parser.add_argument("--slippage", type=int, default=DEFAULT_SLIPPAGE_BPS, help="Slippage in bps")
    parser.add_argument("--quote-only", action="store_true", help="Only get quote, don't swap")

    args = parser.parse_args()

    if args.quote_only:
        try:
            quote = get_quote(args.input_mint, args.output_mint, args.amount, args.slippage)
            print(json.dumps(quote, indent=2))
        except Exception as e:
            print(json.dumps({"error": str(e)}))
    else:
        result = swap(args.input_mint, args.output_mint, args.amount, args.slippage)
        print(json.dumps(result, indent=2))
