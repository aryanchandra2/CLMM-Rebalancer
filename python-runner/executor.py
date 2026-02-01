#!/usr/bin/env python3
"""
TS Script Executor Module

Wrapper for calling TypeScript scripts that interact with the Solana blockchain.
Handles subprocess calls, JSON parsing, error handling, and retries with backoff.
"""

import subprocess
import json
import shutil
import time
import logging
from pathlib import Path
from typing import TypedDict

# Configure logging
logger = logging.getLogger(__name__)

# Path to the TypeScript executor
TS_EXECUTOR = Path(__file__).parent.parent / "ts-executor"


class PositionData(TypedDict):
    positionMint: str
    whirlpool: str
    liquidity: str
    tickLowerIndex: int
    tickUpperIndex: int
    lowerTick: int
    upperTick: int
    currentTick: int
    tickSpacing: int
    inRange: bool
    currentPrice: str
    lowerPrice: str
    upperPrice: str
    feeOwedA: str
    feeOwedB: str


class PoolData(TypedDict):
    whirlpool: str
    mintA: str
    mintB: str
    tickSpacing: int
    currentTick: int
    currentSqrtPrice: str
    currentPrice: str
    liquidity: str
    feeRate: int


class WithdrawResult(TypedDict):
    success: bool
    positionMint: str
    amountAWithdrawn: str
    amountBWithdrawn: str
    feeCollectedA: str
    feeCollectedB: str
    rewardsCollected: list[str]
    txid: str


class OpenPositionResult(TypedDict):
    success: bool
    positionMint: str
    lowerPrice: float
    upperPrice: float
    tokenADeposited: str
    tokenBDeposited: str
    liquidityDelta: str
    txid: str


class ExecutorError(Exception):
    """Exception raised when a TS script execution fails."""

    def __init__(self, message: str, script: str, stderr: str = ""):
        self.script = script
        self.stderr = stderr
        super().__init__(message)


def get_node_command() -> str:
    """Detect the correct node command (node vs node.exe for WSL)."""
    if shutil.which("node"):
        return "node"
    elif shutil.which("node.exe"):
        return "node.exe"
    else:
        raise RuntimeError(
            "Node.js not found. Install Node.js or ensure node.exe is in PATH."
        )


def _run_ts_script(
    script_name: str,
    args: list[str],
    max_retries: int = 3,
    base_delay: float = 2.0,
    timeout: int = 120,
) -> dict:
    """
    Run a TypeScript script with retry logic and exponential backoff.

    Args:
        script_name: Name of the script (without .js extension)
        args: List of arguments to pass to the script
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries (doubles each attempt)
        timeout: Timeout in seconds for each attempt

    Returns:
        Parsed JSON output from the script

    Raises:
        ExecutorError: If all retries fail
    """
    script_path = TS_EXECUTOR / "dist" / f"{script_name}.js"

    if not script_path.exists():
        raise ExecutorError(
            f"TS executor not built. Run: cd {TS_EXECUTOR} && npm install && npm run build",
            script_name,
        )

    node_cmd = get_node_command()
    last_error = None
    last_stderr = ""

    for attempt in range(max_retries):
        try:
            logger.debug(f"Running {script_name} (attempt {attempt + 1}/{max_retries})")

            result = subprocess.run(
                [node_cmd, str(script_path)] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # Check for non-zero exit code
            if result.returncode != 0:
                last_stderr = result.stderr
                # Try to parse error from stdout (TS scripts output JSON errors)
                try:
                    error_data = json.loads(result.stdout)
                    if "error" in error_data:
                        last_error = error_data["error"]
                    else:
                        last_error = result.stderr or "Unknown error"
                except json.JSONDecodeError:
                    last_error = result.stderr or result.stdout or "Unknown error"

                # Don't retry certain errors (e.g., invalid arguments, no liquidity)
                if any(
                    msg in str(last_error).lower()
                    for msg in ["usage:", "invalid", "no liquidity"]
                ):
                    raise ExecutorError(last_error, script_name, last_stderr)

                # Retry on transient errors
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        f"{script_name} failed (attempt {attempt + 1}): {last_error}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    continue

            # Parse JSON output
            output = json.loads(result.stdout)

            # Check for error in response (some scripts return success: false)
            if isinstance(output, dict) and output.get("success") is False:
                error_msg = output.get("error", "Unknown error")

                # Don't retry certain errors
                if any(
                    msg in str(error_msg).lower()
                    for msg in ["usage:", "invalid", "no liquidity", "simulation failed"]
                ):
                    raise ExecutorError(error_msg, script_name)

                # Retry on transient errors
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        f"{script_name} returned error (attempt {attempt + 1}): {error_msg}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    raise ExecutorError(error_msg, script_name)

            return output

        except subprocess.TimeoutExpired:
            last_error = f"Timeout after {timeout}s"
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                logger.warning(
                    f"{script_name} timed out (attempt {attempt + 1}). Retrying in {delay}s..."
                )
                time.sleep(delay)
            continue

        except json.JSONDecodeError as e:
            last_error = f"Failed to parse JSON output: {e}"
            last_stderr = result.stderr if "result" in dir() else ""
            # JSON parse errors are usually not transient
            raise ExecutorError(last_error, script_name, last_stderr)

    # All retries exhausted
    raise ExecutorError(
        f"Failed after {max_retries} attempts: {last_error}",
        script_name,
        last_stderr,
    )


def fetch_position(position_mint: str) -> PositionData:
    """
    Fetch position data from the blockchain.

    Args:
        position_mint: The position mint address

    Returns:
        Position data including ticks, liquidity, and price information
    """
    logger.info(f"Fetching position: {position_mint}")
    result = _run_ts_script("fetch-position", [position_mint])
    return result


def fetch_pool(pool_address: str) -> PoolData:
    """
    Fetch pool data from the blockchain.

    Args:
        pool_address: The Whirlpool address

    Returns:
        Pool data including current price, tick, and liquidity
    """
    logger.info(f"Fetching pool: {pool_address}")
    result = _run_ts_script("fetch-pool", [pool_address])
    return result


def withdraw_all(position_mint: str) -> WithdrawResult:
    """
    Withdraw all liquidity from a position and close it.

    Args:
        position_mint: The position mint address

    Returns:
        Withdrawal result including amounts and transaction ID
    """
    logger.info(f"Withdrawing all from position: {position_mint}")
    result = _run_ts_script(
        "withdraw-all",
        [position_mint],
        max_retries=3,
        timeout=180,  # Longer timeout for transactions
    )
    return result


def open_position(
    pool_address: str,
    lower_price: float,
    upper_price: float,
    amount: int,
    use_token_b: bool = False,
) -> OpenPositionResult:
    """
    Open a new concentrated liquidity position.

    Args:
        pool_address: The Whirlpool address
        lower_price: Lower bound of the price range
        upper_price: Upper bound of the price range
        amount: Amount to deposit (in smallest units)
        use_token_b: If True, amount is in token B; otherwise token A

    Returns:
        Open position result including new position mint and transaction ID
    """
    logger.info(
        f"Opening position on pool {pool_address}: "
        f"range [{lower_price:.4f}, {upper_price:.4f}], "
        f"amount={amount} ({'tokenB' if use_token_b else 'tokenA'})"
    )

    args = [
        pool_address,
        str(lower_price),
        str(upper_price),
        str(amount),
    ]
    if use_token_b:
        args.append("--token-b")

    result = _run_ts_script(
        "open-position",
        args,
        max_retries=3,
        timeout=180,  # Longer timeout for transactions
    )
    return result


# CLI interface for testing
if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test TS script executor")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # fetch-position
    pos_parser = subparsers.add_parser("fetch-position", help="Fetch position data")
    pos_parser.add_argument("mint", help="Position mint address")

    # fetch-pool
    pool_parser = subparsers.add_parser("fetch-pool", help="Fetch pool data")
    pool_parser.add_argument("address", help="Pool address")

    # withdraw-all
    withdraw_parser = subparsers.add_parser("withdraw-all", help="Withdraw all liquidity")
    withdraw_parser.add_argument("mint", help="Position mint address")

    # open-position
    open_parser = subparsers.add_parser("open-position", help="Open new position")
    open_parser.add_argument("pool", help="Pool address")
    open_parser.add_argument("lower", type=float, help="Lower price")
    open_parser.add_argument("upper", type=float, help="Upper price")
    open_parser.add_argument("amount", type=int, help="Token amount")
    open_parser.add_argument("--token-b", action="store_true", help="Use token B amount")

    args = parser.parse_args()

    try:
        if args.command == "fetch-position":
            result = fetch_position(args.mint)
        elif args.command == "fetch-pool":
            result = fetch_pool(args.address)
        elif args.command == "withdraw-all":
            result = withdraw_all(args.mint)
        elif args.command == "open-position":
            result = open_position(
                args.pool, args.lower, args.upper, args.amount, args.token_b
            )
        else:
            parser.print_help()
            exit(1)

        print(json.dumps(result, indent=2))

    except ExecutorError as e:
        print(f"Error: {e}")
        if e.stderr:
            print(f"Stderr: {e.stderr}")
        exit(1)
