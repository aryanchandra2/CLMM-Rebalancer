#!/usr/bin/env python3
"""
Rebalance Module

Decides and executes swaps to achieve target token ratio after withdrawal.
"""

import json
import argparse
from typing import TypedDict

from jupiter import (
    swap,
    get_quote,
    SOL_MINT,
    USDC_MINT,
    DEFAULT_SLIPPAGE_BPS,
)


class RebalanceResult(TypedDict):
    success: bool
    action: str  # "swap_sol_to_usdc", "swap_usdc_to_sol", "no_swap_needed"
    swap_amount: int
    swap_result: dict | None
    error: str | None


# Price fetching (use Jupiter quote as price oracle)
def get_sol_price_in_usdc() -> float:
    """Get current SOL price in USDC using Jupiter quote."""
    # Quote 1 SOL -> USDC
    quote = get_quote(SOL_MINT, USDC_MINT, 1_000_000_000, slippage_bps=10)  # 1 SOL
    out_amount = int(quote["outAmount"])
    # outAmount is in USDC (6 decimals)
    return out_amount / 1_000_000


def calculate_values(
    sol_lamports: int,
    usdc_amount: int,
    sol_price_usdc: float,
) -> tuple[float, float, float]:
    """
    Calculate USD values of holdings.

    Returns:
        (sol_value_usd, usdc_value_usd, total_value_usd)
    """
    sol_amount = sol_lamports / 1_000_000_000  # 9 decimals
    usdc_amount_f = usdc_amount / 1_000_000  # 6 decimals

    sol_value_usd = sol_amount * sol_price_usdc
    usdc_value_usd = usdc_amount_f  # USDC is already USD

    total = sol_value_usd + usdc_value_usd
    return sol_value_usd, usdc_value_usd, total


def calculate_rebalance(
    sol_lamports: int,
    usdc_amount: int,
    target_sol_pct: float = 50.0,
    min_swap_value_usd: float = 1.0,
) -> tuple[str, int]:
    """
    Calculate what swap is needed to achieve target ratio.

    Args:
        sol_lamports: Current SOL balance in lamports
        usdc_amount: Current USDC balance (6 decimals)
        target_sol_pct: Target SOL percentage (0-100)
        min_swap_value_usd: Minimum swap value to execute

    Returns:
        (action, amount) where action is "swap_sol_to_usdc", "swap_usdc_to_sol", or "none"
        and amount is in the input token's smallest units
    """
    sol_price = get_sol_price_in_usdc()
    sol_value, usdc_value, total_value = calculate_values(
        sol_lamports, usdc_amount, sol_price
    )

    if total_value < min_swap_value_usd:
        return "none", 0

    current_sol_pct = (sol_value / total_value) * 100 if total_value > 0 else 0
    target_usdc_pct = 100 - target_sol_pct

    target_sol_value = total_value * (target_sol_pct / 100)
    target_usdc_value = total_value * (target_usdc_pct / 100)

    sol_diff = target_sol_value - sol_value
    usdc_diff = target_usdc_value - usdc_value

    # Determine action
    if abs(sol_diff) < min_swap_value_usd:
        return "none", 0

    if sol_diff > 0:
        # Need more SOL -> swap USDC to SOL
        usdc_to_swap = abs(usdc_diff)
        # Convert USD value to USDC amount (6 decimals)
        usdc_swap_amount = int(usdc_to_swap * 1_000_000)
        return "swap_usdc_to_sol", usdc_swap_amount
    else:
        # Need more USDC -> swap SOL to USDC
        sol_to_swap = abs(sol_diff)
        # Convert USD value to SOL lamports (9 decimals)
        sol_swap_amount = int((sol_to_swap / sol_price) * 1_000_000_000)
        return "swap_sol_to_usdc", sol_swap_amount


def rebalance(
    sol_lamports: int,
    usdc_amount: int,
    target_sol_pct: float = 50.0,
    min_swap_value_usd: float = 1.0,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    dry_run: bool = False,
) -> RebalanceResult:
    """
    Rebalance holdings to target ratio.

    Args:
        sol_lamports: Current SOL balance in lamports
        usdc_amount: Current USDC balance (6 decimals)
        target_sol_pct: Target SOL percentage (0-100)
        min_swap_value_usd: Minimum swap value to execute
        slippage_bps: Slippage tolerance in basis points
        dry_run: If True, only calculate but don't execute swap

    Returns:
        RebalanceResult with action taken and results
    """
    try:
        action, amount = calculate_rebalance(
            sol_lamports, usdc_amount, target_sol_pct, min_swap_value_usd
        )

        if action == "none":
            return {
                "success": True,
                "action": "no_swap_needed",
                "swap_amount": 0,
                "swap_result": None,
                "error": None,
            }

        if dry_run:
            # Get quote only
            if action == "swap_sol_to_usdc":
                quote = get_quote(SOL_MINT, USDC_MINT, amount, slippage_bps)
            else:
                quote = get_quote(USDC_MINT, SOL_MINT, amount, slippage_bps)

            return {
                "success": True,
                "action": action,
                "swap_amount": amount,
                "swap_result": {"quote": quote, "dry_run": True},
                "error": None,
            }

        # Execute swap
        if action == "swap_sol_to_usdc":
            result = swap(SOL_MINT, USDC_MINT, amount, slippage_bps)
        else:
            result = swap(USDC_MINT, SOL_MINT, amount, slippage_bps)

        return {
            "success": result["success"],
            "action": action,
            "swap_amount": amount,
            "swap_result": result,
            "error": result.get("error"),
        }

    except Exception as e:
        return {
            "success": False,
            "action": "error",
            "swap_amount": 0,
            "swap_result": None,
            "error": str(e),
        }


def rebalance_from_withdrawal(
    amount_a_withdrawn: str,
    amount_b_withdrawn: str,
    mint_a: str = SOL_MINT,
    mint_b: str = USDC_MINT,
    target_sol_pct: float = 50.0,
    min_swap_value_usd: float = 1.0,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    dry_run: bool = False,
) -> RebalanceResult:
    """
    Rebalance based on withdrawal output from withdraw-all.

    Args:
        amount_a_withdrawn: Amount of token A withdrawn (string from JSON)
        amount_b_withdrawn: Amount of token B withdrawn (string from JSON)
        mint_a: Token A mint (default SOL)
        mint_b: Token B mint (default USDC)
        target_sol_pct: Target SOL percentage
        min_swap_value_usd: Minimum swap value
        slippage_bps: Slippage tolerance
        dry_run: Only calculate, don't execute

    Returns:
        RebalanceResult
    """
    # Parse amounts
    amount_a = int(amount_a_withdrawn)
    amount_b = int(amount_b_withdrawn)

    # Determine which is SOL and which is USDC
    if mint_a == SOL_MINT:
        sol_lamports = amount_a
        usdc_amount = amount_b
    elif mint_b == SOL_MINT:
        sol_lamports = amount_b
        usdc_amount = amount_a
    else:
        raise ValueError("Neither mint is SOL - cannot determine balances")

    return rebalance(
        sol_lamports=sol_lamports,
        usdc_amount=usdc_amount,
        target_sol_pct=target_sol_pct,
        min_swap_value_usd=min_swap_value_usd,
        slippage_bps=slippage_bps,
        dry_run=dry_run,
    )


# CLI interface
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebalance token holdings")
    parser.add_argument("--sol", type=int, required=True, help="SOL amount in lamports")
    parser.add_argument("--usdc", type=int, required=True, help="USDC amount (6 decimals)")
    parser.add_argument("--target-sol-pct", type=float, default=50.0, help="Target SOL %")
    parser.add_argument("--min-swap-usd", type=float, default=1.0, help="Min swap value USD")
    parser.add_argument("--slippage", type=int, default=DEFAULT_SLIPPAGE_BPS, help="Slippage bps")
    parser.add_argument("--dry-run", action="store_true", help="Don't execute, just show plan")

    args = parser.parse_args()

    result = rebalance(
        sol_lamports=args.sol,
        usdc_amount=args.usdc,
        target_sol_pct=args.target_sol_pct,
        min_swap_value_usd=args.min_swap_usd,
        slippage_bps=args.slippage,
        dry_run=args.dry_run,
    )

    print(json.dumps(result, indent=2))
