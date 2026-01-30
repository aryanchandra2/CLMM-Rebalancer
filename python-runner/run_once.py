#!/usr/bin/env python3
"""
Whirlpool Position Analyzer

Fetches position data from the TypeScript executor and analyzes
whether the position is in range, distance to boundaries, etc.
"""

import subprocess
import json
import argparse
import sys
import shutil
from pathlib import Path

# Path to the TypeScript executor
TS_EXECUTOR = Path(__file__).parent.parent / "ts-executor"


def get_node_command() -> str:
    """Detect the correct node command (node vs node.exe for WSL)."""
    # Check if running in WSL
    if shutil.which("node"):
        return "node"
    elif shutil.which("node.exe"):
        return "node.exe"
    else:
        raise RuntimeError("Node.js not found. Install Node.js or ensure node.exe is in PATH.")


def fetch_position(position_mint: str) -> dict:
    """Call TS executor and parse JSON output."""
    script_path = TS_EXECUTOR / "dist" / "fetch-position.js"

    if not script_path.exists():
        raise RuntimeError(
            f"TS executor not built. Run: cd {TS_EXECUTOR} && npm install && npm run build"
        )

    node_cmd = get_node_command()
    result = subprocess.run(
        [node_cmd, str(script_path), position_mint],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"TS executor failed:\n{result.stderr}")

    return json.loads(result.stdout)


def analyze_position(data: dict) -> dict:
    """Compute decision metrics from position snapshot."""
    current_tick = data["currentTick"]
    lower_tick = data["lowerTick"]
    upper_tick = data["upperTick"]
    tick_spacing = data["tickSpacing"]

    # Distance to boundaries (in ticks)
    dist_to_lower = current_tick - lower_tick
    dist_to_upper = upper_tick - current_tick

    # Range width
    range_width = upper_tick - lower_tick

    # Normalized position within range (0 = at lower, 1 = at upper)
    if range_width > 0:
        position_pct = (dist_to_lower / range_width) * 100
    else:
        position_pct = 0

    # Distance as percentage of range
    dist_to_lower_pct = (dist_to_lower / range_width) * 100 if range_width > 0 else 0
    dist_to_upper_pct = (dist_to_upper / range_width) * 100 if range_width > 0 else 0

    return {
        "in_range": data["inRange"],
        "current_tick": current_tick,
        "lower_tick": lower_tick,
        "upper_tick": upper_tick,
        "dist_to_lower_ticks": dist_to_lower,
        "dist_to_upper_ticks": dist_to_upper,
        "dist_to_lower_pct": round(dist_to_lower_pct, 2),
        "dist_to_upper_pct": round(dist_to_upper_pct, 2),
        "range_width_ticks": range_width,
        "position_pct": round(position_pct, 2),
        "tick_spacing": tick_spacing,
    }


def estimate_position_value(data: dict) -> dict | None:
    """
    Estimate position value (optional).
    This is a simplified estimation - for accurate values,
    use the SDK's decreaseLiquidity quote functions.
    """
    return {
        "liquidity": data["liquidity"],
        "current_price": data["currentPrice"],
        "lower_price": data["lowerPrice"],
        "upper_price": data["upperPrice"],
        "fees_owed_a": data["feeOwedA"],
        "fees_owed_b": data["feeOwedB"],
    }


def print_decision_line(data: dict, analysis: dict):
    """Print single-line decision summary."""
    status = "IN_RANGE" if analysis["in_range"] else "OUT_OF_RANGE"

    # Color codes for terminal
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    status_colored = f"{GREEN}{status}{RESET}" if analysis["in_range"] else f"{RED}{status}{RESET}"

    print(
        f"{status_colored} | "
        f"DIST_TO_LOWER: {analysis['dist_to_lower_ticks']} ticks ({analysis['dist_to_lower_pct']}%) | "
        f"DIST_TO_UPPER: {analysis['dist_to_upper_ticks']} ticks ({analysis['dist_to_upper_pct']}%) | "
        f"POS: {analysis['position_pct']}%"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and analyze Whirlpool position state"
    )
    parser.add_argument(
        "--position",
        required=True,
        help="Position mint address",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print full snapshot and analysis (no actions taken)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON only",
    )
    args = parser.parse_args()

    try:
        # Fetch from chain
        data = fetch_position(args.position)
        analysis = analyze_position(data)
        value_est = estimate_position_value(data)

        if args.json:
            output = {
                "snapshot": data,
                "analysis": analysis,
                "value_estimate": value_est,
            }
            print(json.dumps(output, indent=2))
            return

        if args.dry_run:
            print("=" * 50)
            print("POSITION SNAPSHOT")
            print("=" * 50)
            print(json.dumps(data, indent=2))
            print()
            print("=" * 50)
            print("ANALYSIS")
            print("=" * 50)
            print(json.dumps(analysis, indent=2))
            print()
            if value_est:
                print("=" * 50)
                print("VALUE ESTIMATE")
                print("=" * 50)
                print(json.dumps(value_est, indent=2))
                print()
            print("=" * 50)
            print("DECISION")
            print("=" * 50)

        print_decision_line(data, analysis)

    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Failed to parse TS executor output: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
