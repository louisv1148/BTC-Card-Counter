---
description: BTC HF bot - all HIGH RISK issues fixed, ready for dry-run testing
---

# BTC High-Frequency Trading Bot

## Project Location
`/Users/lvc/.gemini/antigravity/scratch/BTC-Card-Counter`

---

## ✅ HIGH RISK Issues - FIXED

| Issue | Fix Applied |
|-------|-------------|
| DynamoDB shared between dry-run/live | Separate tables: `BTCHFPositions-DryRun` vs `BTCHFPositions` |
| Balance $100 fallback in live mode | Returns None, skips cycle if can't fetch |
| 'resting' orders treated as success | Now waits + cancels, only accepts 'filled' |
| Liquidation without fill verify | Verifies 'filled' status, fails if not |
| Modifying dict while iterating | `list()` copy before iteration |
| Silent exception in is_expired | Logs error, returns True (safe default) |

---

## New Features Added

### 1. Simulated Balance (Dry-Run)
- Starts at $200 (`DRY_RUN_STARTING_BALANCE`)
- Updates with each simulated trade
- Tracks P&L over the session

### 2. Max Exposure Limit
- `MAX_EXPOSURE_FRACTION = 0.50` (50% of bankroll)
- Prevents over-leveraging across all positions
- Shows exposure status each scan

### 3. Slippage Tolerance
- `MAX_SLIPPAGE_CENTS = 5`
- Skips trades if bid-ask spread > 5¢
- Logs spread for each trade opportunity

### 4. Kalshi Position Sync (Live Mode)
- On startup, syncs with actual Kalshi positions
- Warns if tracker has positions not on Kalshi (settled?)
- Warns if Kalshi has positions not in tracker (missed?)

### 5. Order Fill Verification (Live Mode)
- Only accepts 'filled' status
- 'resting' orders: waits up to 30s, then cancels
- Liquidations: verifies fill before closing position tracker

---

## Configuration

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_EDGE_PCT` | 10% | Only trade if 10%+ edge |
| `EXIT_EDGE_PCT` | 1% | Sell when edge drops to 1% |
| `KELLY_FRACTION` | 0.25 | Quarter Kelly sizing |
| `MAX_CONTRACTS` | 10 | Max per single trade |
| `MAX_EXPOSURE_FRACTION` | 0.50 | Max 50% of bankroll at risk |
| `MAX_SLIPPAGE_CENTS` | 5 | Skip if spread > 5¢ |
| `ORDER_TIMEOUT_SEC` | 30 | Cancel resting orders after 30s |
| `DRY_RUN_STARTING_BALANCE` | $200 | Starting simulated balance |
| `REFRESH_INTERVAL_SEC` | 10 | Scan every 10 seconds |
| `TRADING_CUTOFF_MINUTES` | 15 | No new positions in last 15 min |

---

## Running the Bot

### Dry-Run (Safe Testing)
```bash
cd /Users/lvc/.gemini/antigravity/scratch/BTC-Card-Counter/btc
python btc_hf_bot.py --dry-run
```

### Output Shows:
```
######################################################################
# BTC High-Frequency Trading Bot - DRY-RUN 🧪
# Edge threshold: 10.0% | Exit at: 1.0%
# Kelly: 25.0% | Max contracts/trade: 10
# Max exposure: 50.0% of bankroll
# Max slippage: 5¢ spread
# Refresh: 10s | Cutoff: 15 min
# Starting balance: $200.00
# Press Ctrl+C to stop
######################################################################

============================================================
🔍 Scan at 20:30:15 ET (30 min to settlement)
============================================================
📊 BTC: $101,234.56
📈 Volatility (15m): 0.0825%
📋 12 markets for KXBTCD-24DEC1221
💰 Bankroll: $200.00
📊 Exposure: $0.00 / $100.00 (0.0%)
```

---

## What Gets Tracked (Dry-Run Analytics)

### Price Level Outcomes
```
🎯 PRICE BAND OUTCOMES (where does edge break down?):
Price Bucket | Settled | Wins | Losses | Win Rate | Model Accuracy
---------------------------------------------------------------------------
70-79¢      |      30 |   18 |     12 |   60.0% |   65.0% ⚠️
80-89¢      |     100 |   85 |     15 |   85.0% |   88.0% ✅
90-99¢      |      50 |   48 |      2 |   96.0% |   95.0% ✅
```

### Slippage Analysis
```
📉 SLIPPAGE ANALYSIS:
   Average spread: 3.2¢
   Price Bucket | Avg Spread | Spread %
   70-79¢      |       5.1¢ |    6.80%
   80-89¢      |       3.2¢ |    3.76%
```

---

## Still TODO (MEDIUM Risk)

- [ ] Fix naive DST calculation (use `zoneinfo` instead of month check)
- [ ] Add MAX_TOTAL_POSITIONS limit (currently just exposure limit)

---

## Files Changed

| File | Changes |
|------|---------|
| `btc/btc_hf_bot.py` | Major safety updates, balance tracking, slippage |
| `btc/performance_tracker.py` | Price band outcomes, slippage analysis |
| `.agent/workflows/trading-costs-analysis.md` | This file |
