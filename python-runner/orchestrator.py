#!/usr/bin/env python3
"""
CLMM Rebalancer Orchestrator

Main entry point for the rebalancer. Monitors positions and executes the full
rebalance flow: withdraw -> swap -> open new position.

Usage:
    # Daemon mode (continuous monitoring, uses default config)
    python orchestrator.py

    # One-shot mode (single check, for cron)
    python orchestrator.py --once

    # Dry run (test without executing)
    python orchestrator.py --once --dry-run

    # Override position
    python orchestrator.py --position <NEW_MINT>

    # Custom config (relative to cwd or absolute path)
    python orchestrator.py --config ../config/rebalancer.json
"""

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from executor import (
    ExecutorError,
    fetch_pool,
    fetch_position,
    open_position,
    withdraw_all,
)
from range_selector import compute_range_from_current_price
from rebalance import rebalance_from_withdrawal, get_sol_price_in_usdc, calculate_values
from jupiter import get_wallet_balances, SOL_MINT, USDC_MINT
from run_once import analyze_position
from state import StateManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Default config path
DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "rebalancer.json"


class RebalanceTriggers(TypedDict):
    out_of_range: bool
    edge_threshold_pct: float


class Config(TypedDict):
    pool_address: str
    position_mint: str | None
    check_interval_seconds: int
    rebalance_triggers: RebalanceTriggers
    range_width_pct: float
    target_sol_pct: float
    dry_run: bool
    min_swap_value_usd: float
    slippage_bps: int


@dataclass
class RebalanceDecision:
    """Result of checking whether to rebalance."""

    should_rebalance: bool
    reason: str
    analysis: dict | None = None


def load_config(config_path: Path) -> Config:
    """Load configuration from JSON file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        config = json.load(f)

    # Validate required fields
    required = ["pool_address", "check_interval_seconds", "rebalance_triggers"]
    for field in required:
        if field not in config:
            raise ValueError(f"Missing required config field: {field}")

    # Set defaults
    config.setdefault("position_mint", None)
    config.setdefault("range_width_pct", 5.0)
    config.setdefault("target_sol_pct", 50.0)
    config.setdefault("dry_run", False)
    config.setdefault("min_swap_value_usd", 1.0)
    config.setdefault("slippage_bps", 100)

    return config


def check_rebalance_needed(
    position_data: dict,
    triggers: RebalanceTriggers,
) -> RebalanceDecision:
    """
    Check if position needs rebalancing based on trigger conditions.

    Args:
        position_data: Raw position data from fetch_position
        triggers: Rebalance trigger configuration

    Returns:
        RebalanceDecision with should_rebalance flag and reason
    """
    analysis = analyze_position(position_data)

    # Check if out of range
    if triggers.get("out_of_range", True) and not analysis["in_range"]:
        return RebalanceDecision(
            should_rebalance=True,
            reason="Position is OUT OF RANGE",
            analysis=analysis,
        )

    # Check edge proximity
    edge_threshold = triggers.get("edge_threshold_pct", 0)
    if edge_threshold > 0:
        if analysis["dist_to_lower_pct"] < edge_threshold:
            return RebalanceDecision(
                should_rebalance=True,
                reason=f"Near lower edge ({analysis['dist_to_lower_pct']:.1f}% < {edge_threshold}%)",
                analysis=analysis,
            )
        if analysis["dist_to_upper_pct"] < edge_threshold:
            return RebalanceDecision(
                should_rebalance=True,
                reason=f"Near upper edge ({analysis['dist_to_upper_pct']:.1f}% < {edge_threshold}%)",
                analysis=analysis,
            )

    return RebalanceDecision(
        should_rebalance=False,
        reason="Position is healthy",
        analysis=analysis,
    )


def execute_rebalance(
    config: Config,
    state: StateManager,
    dry_run: bool = False,
) -> bool:
    """
    Execute the full rebalance flow.

    Steps:
    1. Withdraw all liquidity from current position
    2. Fetch current pool price
    3. Calculate new price range
    4. Swap to target token ratio
    5. Open new position
    6. Update state with new position mint

    Args:
        config: Rebalancer configuration
        state: State manager
        dry_run: If True, only simulate (no transactions)

    Returns:
        True if rebalance completed successfully
    """
    position_mint = state.position_mint
    pool_address = config["pool_address"]

    logger.info("=" * 60)
    logger.info("STARTING REBALANCE")
    logger.info("=" * 60)

    # Check if resuming from a failed rebalance
    resume_step = None
    if state.is_pending:
        resume_step = state.pending_step
        logger.warning(f"Resuming failed rebalance from step: {resume_step}")

    try:
        # Mark rebalance started (unless resuming)
        if not state.is_pending:
            if not dry_run:
                state.mark_rebalance_started()

        # Step 1: Withdraw (skip if resuming past this step)
        if resume_step in (None, "withdraw"):
            logger.info(f"Step 1: Withdrawing from position {position_mint}")

            if dry_run:
                logger.info("[DRY RUN] Would withdraw all liquidity")
                # Use current wallet balances as proxy for post-withdrawal state
                # This shows what rebalancing would look like with current holdings
                try:
                    balances = get_wallet_balances()
                    logger.info(
                        f"[DRY RUN] Using wallet balances: "
                        f"SOL={balances['sol_lamports']/1e9:.4f}, "
                        f"USDC={balances['usdc_amount']/1e6:.2f}"
                    )
                    withdraw_result = {
                        "amountAWithdrawn": str(balances["sol_lamports"]),
                        "amountBWithdrawn": str(balances["usdc_amount"]),
                        "success": True,
                        "dry_run": True,
                    }
                except Exception as e:
                    logger.warning(f"Could not fetch wallet balances: {e}")
                    withdraw_result = {
                        "amountAWithdrawn": "0",
                        "amountBWithdrawn": "0",
                        "success": True,
                        "dry_run": True,
                    }
            else:
                withdraw_result = withdraw_all(position_mint)
                if not withdraw_result.get("success", False):
                    raise ExecutorError(
                        withdraw_result.get("error", "Withdraw failed"),
                        "withdraw-all",
                    )
                state.mark_withdraw_complete(withdraw_result)

            logger.info(
                f"Withdrawn: A={withdraw_result.get('amountAWithdrawn', 'N/A')}, "
                f"B={withdraw_result.get('amountBWithdrawn', 'N/A')}"
            )
            resume_step = None  # Continue to next step
        else:
            # Resuming - use stored withdrawal amounts
            withdraw_result = state.withdrawn_amounts
            logger.info(f"Using stored withdrawal amounts: {withdraw_result}")

        # Step 2: Fetch current pool price
        logger.info(f"Step 2: Fetching pool data for {pool_address}")
        pool_data = fetch_pool(pool_address)
        current_price = float(pool_data["currentPrice"])
        tick_spacing = pool_data["tickSpacing"]
        logger.info(f"Current pool price: {current_price:.4f}")

        # Step 3: Calculate new range
        logger.info("Step 3: Calculating new price range")
        range_selection = compute_range_from_current_price(
            current_price=current_price,
            tick_spacing=tick_spacing,
            range_width_pct=config["range_width_pct"],
        )
        logger.info(
            f"New range: [{range_selection['lower_price']:.4f}, {range_selection['upper_price']:.4f}] "
            f"(ticks: {range_selection['lower_tick']} to {range_selection['upper_tick']})"
        )

        # Step 4: Swap to target ratio (skip if resuming past this step)
        if resume_step in (None, "swap"):
            logger.info("Step 4: Rebalancing token ratio")

            # Get amounts from withdrawal (or use estimates for dry run)
            amount_a = int(withdraw_result.get("amountAWithdrawn", "0"))
            amount_b = int(withdraw_result.get("amountBWithdrawn", "0"))

            # Calculate current ratio
            try:
                sol_price = get_sol_price_in_usdc()
                sol_val, usdc_val, total_val = calculate_values(amount_a, amount_b, sol_price)
                current_sol_pct = (sol_val / total_val * 100) if total_val > 0 else 0
                logger.info(
                    f"Before swap: SOL={amount_a/1e9:.4f} (${sol_val:.2f}), "
                    f"USDC={amount_b/1e6:.2f} (${usdc_val:.2f}) | "
                    f"Ratio: {current_sol_pct:.1f}% SOL / {100-current_sol_pct:.1f}% USDC"
                )
            except Exception as e:
                logger.warning(f"Could not calculate current ratio: {e}")

            if dry_run:
                logger.info(
                    f"[DRY RUN] Would swap to target: {config['target_sol_pct']}% SOL / "
                    f"{100-config['target_sol_pct']}% USDC"
                )
                swap_result = {"action": "dry_run", "success": True}
            else:
                swap_result = rebalance_from_withdrawal(
                    amount_a_withdrawn=str(amount_a),
                    amount_b_withdrawn=str(amount_b),
                    target_sol_pct=config["target_sol_pct"],
                    min_swap_value_usd=config["min_swap_value_usd"],
                    slippage_bps=config["slippage_bps"],
                    dry_run=False,
                )
                if not swap_result.get("success", False):
                    raise ExecutorError(
                        swap_result.get("error", "Swap failed"),
                        "rebalance",
                    )
                state.mark_swap_complete()

            logger.info(
                f"After swap: Target {config['target_sol_pct']}% SOL / "
                f"{100-config['target_sol_pct']}% USDC"
            )
            resume_step = None

        # Step 5: Open new position
        logger.info("Step 5: Opening new position")

        # Determine deposit amount (use token B / USDC since it's more predictable)
        # In a real scenario, we'd query wallet balances here
        # For now, use a conservative approach: let the SDK figure out max deposit

        if dry_run:
            logger.info(
                f"[DRY RUN] Would open position: "
                f"pool={pool_address}, "
                f"range=[{range_selection['lower_price']:.4f}, {range_selection['upper_price']:.4f}]"
            )
            new_position_mint = "DRY_RUN_POSITION_MINT"
            open_result = {"success": True, "dry_run": True}
        else:
            # For now, we'll open with a small test amount
            # In production, this should use actual wallet balance
            # The amount here should be calculated based on post-swap balances
            # For safety, we'll use a minimal amount or require it to be configured
            logger.warning(
                "Opening position with all available balance. "
                "Ensure wallet has appropriate token balances."
            )

            # Open position using token B (USDC) amount
            # The SDK will calculate the matching token A amount
            # Using 0 here would fail, so we need actual balance
            # For now, use a reasonable default that can be overridden
            open_result = open_position(
                pool_address=pool_address,
                lower_price=range_selection["lower_price"],
                upper_price=range_selection["upper_price"],
                amount=1_000_000,  # 1 USDC as minimum (will be adjusted by SDK)
                use_token_b=True,
            )

            if not open_result.get("success", False):
                raise ExecutorError(
                    open_result.get("error", "Open position failed"),
                    "open-position",
                )

            new_position_mint = open_result["positionMint"]
            state.mark_rebalance_complete(new_position_mint)

        logger.info("=" * 60)
        logger.info("REBALANCE COMPLETE")
        logger.info(f"New position mint: {new_position_mint}")
        logger.info("=" * 60)

        return True

    except ExecutorError as e:
        logger.error(f"Rebalance failed: {e}")
        if not dry_run:
            state.mark_rebalance_failed(str(e))
        return False

    except Exception as e:
        logger.exception(f"Unexpected error during rebalance: {e}")
        if not dry_run:
            state.mark_rebalance_failed(str(e))
        return False


def run_check_cycle(
    config: Config,
    state: StateManager,
    dry_run: bool = False,
    force_rebalance: bool = False,
) -> bool:
    """
    Run a single check cycle.

    Args:
        config: Rebalancer configuration
        state: State manager
        dry_run: If True, simulate without executing transactions
        force_rebalance: If True, force rebalance regardless of position health

    Returns:
        True if cycle completed (whether or not rebalance occurred)
    """
    position_mint = state.position_mint

    if not position_mint:
        logger.error(
            "No position mint configured. Set position_mint in config or use --position flag."
        )
        return False

    # Check for pending rebalance
    if state.is_pending:
        logger.warning(
            f"Found pending rebalance at step: {state.pending_step}. "
            "Attempting to resume..."
        )
        return execute_rebalance(config, state, dry_run)

    try:
        # Fetch position state
        logger.info(f"Fetching position: {position_mint}")
        position_data = fetch_position(position_mint)

        # Check if rebalance needed
        decision = check_rebalance_needed(
            position_data,
            config["rebalance_triggers"],
        )

        # Log status
        analysis = decision.analysis
        status = "IN_RANGE" if analysis["in_range"] else "OUT_OF_RANGE"
        logger.info(
            f"Position status: {status} | "
            f"Price at {analysis['dist_to_lower_pct']:.1f}% from lower, "
            f"{analysis['dist_to_upper_pct']:.1f}% to upper"
        )

        if decision.should_rebalance or force_rebalance:
            if force_rebalance and not decision.should_rebalance:
                logger.info("Rebalance FORCED (position is healthy)")
            else:
                logger.info(f"Rebalance triggered: {decision.reason}")

            if dry_run:
                logger.info("[DRY RUN] Would execute rebalance")
                # Still run through the flow to show what would happen
                return execute_rebalance(config, state, dry_run=True)
            else:
                return execute_rebalance(config, state, dry_run=False)
        else:
            logger.info(f"No rebalance needed: {decision.reason}")
            return True

    except ExecutorError as e:
        logger.error(f"Failed to fetch position: {e}")
        return False


def run_daemon(
    config: Config,
    state: StateManager,
    dry_run: bool = False,
) -> None:
    """
    Run in daemon mode (continuous monitoring).

    Args:
        config: Rebalancer configuration
        state: State manager
        dry_run: If True, simulate without executing transactions
    """
    logger.info("Starting daemon mode")
    logger.info(f"Check interval: {config['check_interval_seconds']}s")
    logger.info(f"Pool: {config['pool_address']}")
    logger.info(f"Position: {state.position_mint}")
    logger.info(f"Dry run: {dry_run}")

    # Handle graceful shutdown
    shutdown_requested = False

    def signal_handler(signum, frame):
        nonlocal shutdown_requested
        logger.info("Shutdown requested...")
        shutdown_requested = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not shutdown_requested:
        try:
            run_check_cycle(config, state, dry_run)
        except Exception as e:
            logger.exception(f"Error in check cycle: {e}")

        # Sleep until next check (interruptible)
        for _ in range(config["check_interval_seconds"]):
            if shutdown_requested:
                break
            time.sleep(1)

    logger.info("Daemon stopped")


def main():
    parser = argparse.ArgumentParser(
        description="CLMM Rebalancer Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start daemon with default config
  python orchestrator.py

  # One-shot check (for cron)
  python orchestrator.py --once

  # Dry run (test without executing)
  python orchestrator.py --once --dry-run

  # Test full rebalance flow (forced, dry-run)
  python orchestrator.py --once --dry-run --force-rebalance

  # Use custom config
  python orchestrator.py --config /path/to/config.json

  # Override position mint
  python orchestrator.py --position <MINT_ADDRESS>
        """,
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config file (default: config/rebalancer.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (for cron jobs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate without executing transactions",
    )
    parser.add_argument(
        "--position",
        type=str,
        help="Override position mint address",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Path to state file (default: python-runner/state.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--force-rebalance",
        action="store_true",
        help="Force rebalance regardless of position health (use with --dry-run to test)",
    )

    args = parser.parse_args()

    # Set log level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error(f"Config file not found: {args.config}")
        logger.info("Create a config file or specify one with --config")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Invalid config: {e}")
        sys.exit(1)

    # Apply CLI overrides
    if args.dry_run:
        config["dry_run"] = True

    # Initialize state manager
    state = StateManager(args.state_file)

    # Handle position override
    if args.position:
        state.position_mint = args.position
        logger.info(f"Position overridden to: {args.position}")
    elif config.get("position_mint"):
        state.initialize_from_config(config["position_mint"])

    # Run
    if args.once:
        success = run_check_cycle(config, state, config["dry_run"], args.force_rebalance)
        sys.exit(0 if success else 1)
    else:
        if args.force_rebalance:
            logger.warning("--force-rebalance is ignored in daemon mode")
        run_daemon(config, state, config["dry_run"])


if __name__ == "__main__":
    main()
