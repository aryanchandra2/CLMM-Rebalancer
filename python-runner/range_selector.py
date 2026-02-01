#!/usr/bin/env python3
"""
Range Selector Module

Computes optimal price range for new Whirlpool positions based on
current price and volatility regime.
"""

import json
import math
from typing import TypedDict


class RangeSelection(TypedDict):
    lower_price: float
    upper_price: float
    lower_tick: int
    upper_tick: int
    range_width_pct: float
    current_price: float


# Tick spacing for common Whirlpool fee tiers
# tickSpacing=1: 0.01% fee tier
# tickSpacing=8: 0.05% fee tier
# tickSpacing=64: 0.30% fee tier
# tickSpacing=128: 1.00% fee tier


def tick_to_price(tick: int, decimals_a: int = 9, decimals_b: int = 6) -> float:
    """
    Convert tick index to human-readable price.

    Args:
        tick: Tick index
        decimals_a: Decimals of token A (default 9 for SOL)
        decimals_b: Decimals of token B (default 6 for USDC)

    Returns:
        Human-readable price (tokenB per tokenA)
    """
    raw_price = math.pow(1.0001, tick)
    # Reverse the decimal adjustment
    decimal_adjustment = 10 ** (decimals_b - decimals_a)
    return raw_price / decimal_adjustment


def price_to_tick(price: float, decimals_a: int = 9, decimals_b: int = 6) -> int:
    """
    Convert human-readable price to tick index (unrounded).

    Args:
        price: Human-readable price (tokenB per tokenA, e.g., 103 USDC/SOL)
        decimals_a: Decimals of token A (default 9 for SOL)
        decimals_b: Decimals of token B (default 6 for USDC)
    """
    if price <= 0:
        raise ValueError("Price must be positive")
    # Adjust price for decimal difference
    decimal_adjustment = 10 ** (decimals_b - decimals_a)
    adjusted_price = price * decimal_adjustment
    return int(math.floor(math.log(adjusted_price) / math.log(1.0001)))


def round_tick_down(tick: int, tick_spacing: int) -> int:
    """Round tick down to nearest valid tick for the pool's tick spacing."""
    return (tick // tick_spacing) * tick_spacing


def round_tick_up(tick: int, tick_spacing: int) -> int:
    """Round tick up to nearest valid tick for the pool's tick spacing."""
    remainder = tick % tick_spacing
    if remainder == 0:
        return tick
    return tick + (tick_spacing - remainder)


def compute_range_from_current_price(
    current_price: float,
    tick_spacing: int,
    range_width_pct: float = 5.0,
    decimals_a: int = 9,
    decimals_b: int = 6,
) -> RangeSelection:
    """
    Compute a symmetric price range around the current price.

    Args:
        current_price: Current pool price (tokenB per tokenA)
        tick_spacing: Pool's tick spacing
        range_width_pct: Range width as percentage of current price (each side)
                         e.g., 5.0 means ±5% = 10% total range
        decimals_a: Decimals of token A (default 9 for SOL)
        decimals_b: Decimals of token B (default 6 for USDC)

    Returns:
        RangeSelection with lower/upper prices and ticks
    """
    # Compute target prices
    lower_price_target = current_price * (1 - range_width_pct / 100)
    upper_price_target = current_price * (1 + range_width_pct / 100)

    # Convert to ticks (with decimal adjustment)
    lower_tick_raw = price_to_tick(lower_price_target, decimals_a, decimals_b)
    upper_tick_raw = price_to_tick(upper_price_target, decimals_a, decimals_b)

    # Round to valid ticks (lower rounds down, upper rounds up for safety)
    lower_tick = round_tick_down(lower_tick_raw, tick_spacing)
    upper_tick = round_tick_up(upper_tick_raw, tick_spacing)

    # Ensure minimum distance
    if upper_tick <= lower_tick:
        upper_tick = lower_tick + tick_spacing

    # Convert back to actual prices
    lower_price = tick_to_price(lower_tick, decimals_a, decimals_b)
    upper_price = tick_to_price(upper_tick, decimals_a, decimals_b)

    # Calculate actual range width
    actual_range_pct = ((upper_price - lower_price) / current_price) * 100

    return {
        "lower_price": lower_price,
        "upper_price": upper_price,
        "lower_tick": lower_tick,
        "upper_tick": upper_tick,
        "range_width_pct": round(actual_range_pct, 2),
        "current_price": current_price,
    }


def compute_range_from_ticks(
    current_tick: int,
    tick_spacing: int,
    ticks_each_side: int = 50,
    decimals_a: int = 9,
    decimals_b: int = 6,
) -> RangeSelection:
    """
    Compute a range using tick offsets from current tick.

    Args:
        current_tick: Current pool tick
        tick_spacing: Pool's tick spacing
        ticks_each_side: Number of ticks on each side of current
        decimals_a: Decimals of token A (default 9 for SOL)
        decimals_b: Decimals of token B (default 6 for USDC)

    Returns:
        RangeSelection with lower/upper prices and ticks
    """
    # Round the offset to be a multiple of tick_spacing
    tick_offset = (ticks_each_side // tick_spacing) * tick_spacing
    if tick_offset == 0:
        tick_offset = tick_spacing

    # Round current tick to nearest valid tick
    current_tick_rounded = round_tick_down(current_tick, tick_spacing)

    lower_tick = current_tick_rounded - tick_offset
    upper_tick = current_tick_rounded + tick_offset

    current_price = tick_to_price(current_tick, decimals_a, decimals_b)
    lower_price = tick_to_price(lower_tick, decimals_a, decimals_b)
    upper_price = tick_to_price(upper_tick, decimals_a, decimals_b)

    range_width_pct = ((upper_price - lower_price) / current_price) * 100

    return {
        "lower_price": lower_price,
        "upper_price": upper_price,
        "lower_tick": lower_tick,
        "upper_tick": upper_tick,
        "range_width_pct": round(range_width_pct, 2),
        "current_price": current_price,
    }


# Volatility regime -> range width mapping
VOLATILITY_RANGE_MAP = {
    "low": 2.0,      # ±2% = 4% total range (tight, more fees)
    "medium": 5.0,   # ±5% = 10% total range (balanced)
    "high": 10.0,    # ±10% = 20% total range (wide, less rebalancing)
    "extreme": 20.0, # ±20% = 40% total range (very wide)
}


def compute_range_for_volatility(
    current_price: float,
    tick_spacing: int,
    volatility_regime: str = "medium",
    decimals_a: int = 9,
    decimals_b: int = 6,
) -> RangeSelection:
    """
    Compute range based on volatility regime.

    Args:
        current_price: Current pool price
        tick_spacing: Pool's tick spacing
        volatility_regime: "low", "medium", "high", or "extreme"
        decimals_a: Decimals of token A (default 9 for SOL)
        decimals_b: Decimals of token B (default 6 for USDC)

    Returns:
        RangeSelection with appropriate range width
    """
    range_width = VOLATILITY_RANGE_MAP.get(volatility_regime, VOLATILITY_RANGE_MAP["medium"])
    return compute_range_from_current_price(current_price, tick_spacing, range_width, decimals_a, decimals_b)


# CLI interface
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute position price range")
    parser.add_argument("--current-price", type=float, required=True, help="Current pool price")
    parser.add_argument("--tick-spacing", type=int, required=True, help="Pool tick spacing")
    parser.add_argument("--range-width", type=float, default=5.0, help="Range width %% each side")
    parser.add_argument("--volatility", choices=["low", "medium", "high", "extreme"],
                        help="Use volatility preset instead of --range-width")
    parser.add_argument("--decimals-a", type=int, default=9, help="Decimals of token A (default: 9 for SOL)")
    parser.add_argument("--decimals-b", type=int, default=6, help="Decimals of token B (default: 6 for USDC)")

    args = parser.parse_args()

    if args.volatility:
        result = compute_range_for_volatility(
            args.current_price,
            args.tick_spacing,
            args.volatility,
            args.decimals_a,
            args.decimals_b,
        )
    else:
        result = compute_range_from_current_price(
            args.current_price,
            args.tick_spacing,
            args.range_width,
            args.decimals_a,
            args.decimals_b,
        )

    print(json.dumps(result, indent=2))
