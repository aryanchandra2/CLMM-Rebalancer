# CLMM Rebalancer

Automated CLMM liquidity manager for Solana that monitors Orca Whirlpool positions and rebalances tick ranges.

## Overview

The rebalancer monitors your concentrated liquidity position and automatically rebalances when:
- Position goes **out of range** (price moved outside your bounds)
- Position is **near the edge** (price approaching a boundary)

### Rebalance Flow

1. **Withdraw** - Close the out-of-range position, get back SOL + USDC
2. **Swap** - Rebalance tokens to 50/50 ratio (configurable)
3. **Open** - Create new position centered on current price


## Installation

### 1. Python Runner

```bash
cd python-runner
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

### 2. TypeScript Executor

```bash
cd ts-executor
npm install
npm run build
```

### 3. Environment Variables

Create `.env` in the project root:

```env
SOLANA_RPC_URL=https://your-rpc-endpoint.com
SOLANA_PRIVATE_KEY=your-base58-private-key
JUPITER_API_KEY=jupiter-api-key
```

## Configuration

Edit `python-runner/config/rebalancer.json`:

```json
{
  "pool_address": "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",
  "position_mint": null,
  "check_interval_seconds": 60,
  "rebalance_triggers": {
    "out_of_range": true,
    "edge_threshold_pct": 10.0
  },
  "range_width_pct": 5.0,
  "target_sol_pct": 50.0,
  "dry_run": false,
  "min_swap_value_usd": 1.0,
  "slippage_bps": 100
}
```

### Config Options

| Option | Description | Default |
|--------|-------------|---------|
| `pool_address` | Whirlpool pool address | Required |
| `position_mint` | Position mint address (or use `--position` flag) | `null` |
| `check_interval_seconds` | Seconds between checks in daemon mode | `60` |
| `rebalance_triggers.out_of_range` | Trigger when position is out of range | `true` |
| `rebalance_triggers.edge_threshold_pct` | Trigger when price is within X% of edge | `10.0` |
| `range_width_pct` | New position range width (±X% from current price) | `5.0` |
| `target_sol_pct` | Target SOL percentage after swap (50 = 50/50) | `50.0` |
| `dry_run` | Simulate without executing transactions | `false` |
| `min_swap_value_usd` | Minimum swap value to execute | `1.0` |
| `slippage_bps` | Slippage tolerance in basis points | `100` |

## Usage

All commands run from `python-runner/` directory.

### Orchestrator

```bash
# Start daemon mode (continuous monitoring)
python orchestrator.py

# One-shot check (for cron jobs)
python orchestrator.py --once

# Dry run - simulate without executing transactions
python orchestrator.py --once --dry-run

# Force rebalance (test the full flow)
python orchestrator.py --once --dry-run --force-rebalance

# Use custom config file
python orchestrator.py --config /path/to/config.json

# Override position mint
python orchestrator.py --position <MINT_ADDRESS>

# Custom state file location
python orchestrator.py --state-file /path/to/state.json

# Enable debug logging
python orchestrator.py --debug
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to config file (default: `config/rebalancer.json`) |
| `--once` | Run once and exit (for cron jobs) |
| `--dry-run` | Simulate without executing transactions |
| `--force-rebalance` | Force rebalance regardless of position health |
| `--position MINT` | Override position mint address |
| `--state-file PATH` | Path to state file (default: `state.json`) |
| `--debug` | Enable debug logging |

### State Management

```bash
# Show current state
python state.py show

# Set position mint manually
python state.py set-position <MINT_ADDRESS>

# Clear pending rebalance (after manual recovery)
python state.py reset-pending

# Reset all state
python state.py reset-all
```

### Executor (TS Script Wrapper)

```bash
# Fetch position data
python executor.py fetch-position <POSITION_MINT>

# Fetch pool data
python executor.py fetch-pool <POOL_ADDRESS>

# Withdraw all liquidity (CAUTION: real transaction)
python executor.py withdraw-all <POSITION_MINT>

# Open new position (CAUTION: real transaction)
python executor.py open-position <POOL> <LOWER_PRICE> <UPPER_PRICE> <AMOUNT> [--token-b]
```

### Other Utilities

```bash
# Analyze position (one-time check)
python run_once.py --position <MINT> --dry-run

# Calculate price range
python range_selector.py --current-price 100 --tick-spacing 64 --range-width 5

# Test Jupiter swap (quote only)
python jupiter.py --input-mint <MINT> --output-mint <MINT> --amount <AMOUNT> --quote-only
```

### State Recovery

If a rebalance fails midway, the state file tracks progress:

```json
{
  "current_position_mint": "...",
  "last_rebalance": "2024-01-31T12:00:00Z",
  "pending_rebalance": true,
  "pending_step": "swap",
  "withdrawn_amounts": { ... }
}
```

On next run, the orchestrator resumes from the failed step.
