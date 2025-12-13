#!/bin/bash
#
# Run the BTC High-Frequency Trading Bot
#
# Usage:
#   ./run_hf_bot.sh              # Live trading (DANGER!)
#   ./run_hf_bot.sh --dry-run    # Dry-run mode (safe)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BTC_DIR="$(dirname "$SCRIPT_DIR")"

# Check for required environment variables in live mode
if [[ "$1" != "--dry-run" ]]; then
    echo "⚠️  LIVE TRADING MODE"
    echo ""
    
    if [[ -z "$KALSHI_KEY_ID" || -z "$KALSHI_PRIVATE_KEY" ]]; then
        echo "ERROR: KALSHI_KEY_ID and KALSHI_PRIVATE_KEY must be set for live trading"
        echo ""
        echo "Set them with:"
        echo "  export KALSHI_KEY_ID='your-key-id'"
        echo "  export KALSHI_PRIVATE_KEY='your-private-key'"
        echo ""
        echo "Or run with --dry-run for testing"
        exit 1
    fi
    
    # Confirmation
    read -p "Are you sure you want to start LIVE trading? (yes/no): " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Aborted."
        exit 0
    fi
else
    echo "🧪 DRY-RUN MODE (no real trades)"
    echo ""
fi

# Check for AWS credentials (needed for DynamoDB volatility data)
if ! aws sts get-caller-identity &>/dev/null; then
    echo "WARNING: AWS credentials not configured. Volatility data may not be available."
    echo ""
fi

# Activate virtual environment if it exists
if [[ -f "$BTC_DIR/venv/bin/activate" ]]; then
    source "$BTC_DIR/venv/bin/activate"
fi

# Run the bot
cd "$BTC_DIR"
python btc_hf_bot.py "$@"
