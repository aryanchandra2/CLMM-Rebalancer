#!/usr/bin/env python3
"""
State Persistence Module

Tracks position state across rebalance cycles:
- Current position mint
- Last rebalance timestamp
- Pending rebalance flag (for recovery from partial failures)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# Default state file location
DEFAULT_STATE_FILE = Path(__file__).parent / "state.json"


class RebalanceState(TypedDict):
    current_position_mint: str | None
    last_rebalance: str | None  # ISO 8601 timestamp
    pending_rebalance: bool
    pending_step: str | None  # "withdraw", "swap", "open_position"
    withdrawn_amounts: dict | None  # Stored for recovery


class StateManager:
    """
    Manages persistent state for the rebalancer.

    State is stored in a JSON file and includes:
    - current_position_mint: The active position's mint address
    - last_rebalance: ISO 8601 timestamp of last successful rebalance
    - pending_rebalance: True if a rebalance failed midway
    - pending_step: Which step to resume from ("withdraw", "swap", "open_position")
    - withdrawn_amounts: Token amounts after withdrawal (for swap recovery)
    """

    def __init__(self, state_file: Path | str | None = None):
        """
        Initialize the state manager.

        Args:
            state_file: Path to the state file. Defaults to python-runner/state.json
        """
        self.state_file = Path(state_file) if state_file else DEFAULT_STATE_FILE
        self._state: RebalanceState = self._load_or_create()

    def _load_or_create(self) -> RebalanceState:
        """Load existing state or create a new one."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    logger.info(f"Loaded state from {self.state_file}")
                    return self._validate_state(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Invalid state file, creating new: {e}")
                return self._default_state()
        else:
            logger.info(f"Creating new state file: {self.state_file}")
            state = self._default_state()
            self._save(state)
            return state

    def _default_state(self) -> RebalanceState:
        """Return default empty state."""
        return {
            "current_position_mint": None,
            "last_rebalance": None,
            "pending_rebalance": False,
            "pending_step": None,
            "withdrawn_amounts": None,
        }

    def _validate_state(self, data: dict) -> RebalanceState:
        """Validate and normalize loaded state."""
        return {
            "current_position_mint": data.get("current_position_mint"),
            "last_rebalance": data.get("last_rebalance"),
            "pending_rebalance": data.get("pending_rebalance", False),
            "pending_step": data.get("pending_step"),
            "withdrawn_amounts": data.get("withdrawn_amounts"),
        }

    def _save(self, state: RebalanceState | None = None) -> None:
        """Save state to disk."""
        if state is None:
            state = self._state

        # Ensure parent directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
        logger.debug(f"State saved to {self.state_file}")

    @property
    def position_mint(self) -> str | None:
        """Get the current position mint."""
        return self._state["current_position_mint"]

    @position_mint.setter
    def position_mint(self, value: str | None) -> None:
        """Set the current position mint and save."""
        self._state["current_position_mint"] = value
        self._save()

    @property
    def last_rebalance(self) -> datetime | None:
        """Get the last rebalance timestamp."""
        ts = self._state["last_rebalance"]
        if ts:
            return datetime.fromisoformat(ts)
        return None

    @property
    def is_pending(self) -> bool:
        """Check if a rebalance is pending (failed midway)."""
        return self._state["pending_rebalance"]

    @property
    def pending_step(self) -> str | None:
        """Get the step to resume from."""
        return self._state["pending_step"]

    @property
    def withdrawn_amounts(self) -> dict | None:
        """Get stored withdrawal amounts for recovery."""
        return self._state["withdrawn_amounts"]

    def mark_rebalance_started(self) -> None:
        """Mark that a rebalance has started."""
        self._state["pending_rebalance"] = True
        self._state["pending_step"] = "withdraw"
        self._save()
        logger.info("Rebalance started")

    def mark_withdraw_complete(self, amounts: dict) -> None:
        """
        Mark withdraw step complete and store amounts.

        Args:
            amounts: Dict with amountAWithdrawn, amountBWithdrawn, etc.
        """
        self._state["pending_step"] = "swap"
        self._state["withdrawn_amounts"] = amounts
        # Clear position mint since position is closed
        self._state["current_position_mint"] = None
        self._save()
        logger.info("Withdraw complete, proceeding to swap")

    def mark_swap_complete(self) -> None:
        """Mark swap step complete."""
        self._state["pending_step"] = "open_position"
        self._save()
        logger.info("Swap complete, proceeding to open position")

    def mark_rebalance_complete(self, new_position_mint: str) -> None:
        """
        Mark rebalance fully complete.

        Args:
            new_position_mint: The mint address of the new position
        """
        self._state["current_position_mint"] = new_position_mint
        self._state["last_rebalance"] = datetime.now(timezone.utc).isoformat()
        self._state["pending_rebalance"] = False
        self._state["pending_step"] = None
        self._state["withdrawn_amounts"] = None
        self._save()
        logger.info(f"Rebalance complete. New position: {new_position_mint}")

    def mark_rebalance_failed(self, error: str) -> None:
        """
        Mark that the rebalance failed (keeps pending state for recovery).

        Args:
            error: Error message for logging
        """
        logger.error(f"Rebalance failed at step '{self._state['pending_step']}': {error}")
        # Keep pending state as-is for manual recovery

    def reset_pending(self) -> None:
        """Reset pending state (for manual recovery or after fixing issues)."""
        self._state["pending_rebalance"] = False
        self._state["pending_step"] = None
        self._state["withdrawn_amounts"] = None
        self._save()
        logger.info("Pending state cleared")

    def get_state(self) -> RebalanceState:
        """Get a copy of the current state."""
        return self._state.copy()

    def initialize_from_config(self, position_mint: str | None) -> None:
        """
        Initialize state from config if no position is tracked.

        Args:
            position_mint: Position mint from config file
        """
        if self._state["current_position_mint"] is None and position_mint:
            self._state["current_position_mint"] = position_mint
            self._save()
            logger.info(f"Initialized position from config: {position_mint}")


# CLI interface for testing and manual state management
if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Manage rebalancer state")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Path to state file",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # show
    subparsers.add_parser("show", help="Show current state")

    # set-position
    set_pos = subparsers.add_parser("set-position", help="Set position mint")
    set_pos.add_argument("mint", help="Position mint address")

    # reset-pending
    subparsers.add_parser("reset-pending", help="Clear pending rebalance state")

    # reset-all
    subparsers.add_parser("reset-all", help="Reset all state")

    args = parser.parse_args()

    state = StateManager(args.state_file)

    if args.command == "show":
        print(json.dumps(state.get_state(), indent=2))

    elif args.command == "set-position":
        state.position_mint = args.mint
        print(f"Position set to: {args.mint}")

    elif args.command == "reset-pending":
        state.reset_pending()
        print("Pending state cleared")

    elif args.command == "reset-all":
        state._state = state._default_state()
        state._save()
        print("State reset to defaults")

    else:
        parser.print_help()
