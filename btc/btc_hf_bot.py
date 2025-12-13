#!/usr/bin/env python3
"""
BTC High-Frequency Trading Bot

Continuous trading bot for Kalshi BTC hourly markets:
- 10% edge threshold for entry
- Quarter Kelly (0.25) position sizing  
- 10 contract max per trade
- 50% max exposure of bankroll
- 10-second refresh cycle
- 5¢ max slippage tolerance
- Liquidation when edge drops to 1%
- 5pp edge increase required to add to existing position

Usage:
    python btc_hf_bot.py --dry-run     # Test with simulated $200 balance
    python btc_hf_bot.py               # Live trading (requires Kalshi API keys)
"""


import argparse
import json
import math
import os
import sys
import time
import signal
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from performance_tracker import PerformanceTracker, TradeRecord, TradeAction

# Try to import Kalshi client (may fail in dry-run without proper setup)
try:
    from lambda_package.kalshi_client import KalshiClient
    KALSHI_AVAILABLE = True
except ImportError:
    KALSHI_AVAILABLE = False
    print("[WARNING] KalshiClient not available - dry-run only")


# =============================================================================
# CONFIGURATION
# =============================================================================

# Edge threshold - only trade if NET edge (after fees) shows this % edge
MIN_EDGE_PCT = 10.0

# Exit threshold - sell when edge drops to this level
EXIT_EDGE_PCT = 1.0

# Kalshi trading fee rate (7% of potential payout, per Kalshi docs)
# Fee formula: ceil(0.07 × contracts × price_cents × (1 - price_cents/100))
KALSHI_FEE_RATE = 0.07

# Kelly fraction - quarter Kelly for safety
KELLY_FRACTION = 0.25

# Maximum contracts per single trade
MAX_CONTRACTS = 10

# Maximum total exposure - never risk more than this fraction of bankroll
MAX_EXPOSURE_FRACTION = 0.50  # 50% of balance max across all positions

# Minimum edge increase (percentage points) to add to existing position
EDGE_INCREASE_THRESHOLD = 5.0

# Maximum slippage tolerance in cents - skip trade if spread exceeds this
MAX_SLIPPAGE_CENTS = 5  # If bid-ask spread > 5 cents, skip


# Refresh interval in seconds
REFRESH_INTERVAL_SEC = 10

# Trading cutoff - stop opening NEW positions when this many minutes remain
TRADING_CUTOFF_MINUTES = 15

# Order timeout - cancel resting orders after this many seconds (live mode)
ORDER_TIMEOUT_SEC = 30

# Starting balance for dry-run mode
DRY_RUN_STARTING_BALANCE = 200.0

# Kalshi BTC series
BTC_SERIES = "KXBTCD"

# DynamoDB tables
VOL_TABLE = "BTCPriceHistory"
POSITION_TABLE = "BTCHFPositions"
POSITION_TABLE_DRYRUN = "BTCHFPositions-DryRun"  # Separate table for dry-run


@dataclass
class Position:
    """Tracks an open position."""
    ticker: str
    contracts: int
    avg_price_cents: float
    entry_edge: float
    last_edge: float
    btc_price_at_entry: float
    strike_price: float
    opened_at: str
    expiry_time: str  # ISO format - when the contract settles (top of next hour)
    
    def total_cost(self) -> float:
        """Total cost in dollars."""
        return self.contracts * self.avg_price_cents / 100
    
    def potential_profit(self) -> float:
        """Potential profit if NO wins, in dollars."""
        return self.contracts * (100 - self.avg_price_cents) / 100
    
    def is_expired(self) -> bool:
        """Check if this position's contract has already settled."""
        try:
            expiry = datetime.fromisoformat(self.expiry_time)
            return datetime.utcnow() > expiry
        except Exception as e:
            # If we can't parse expiry, treat as expired (safe default)
            print(f"[WARNING] Could not parse expiry_time '{self.expiry_time}': {e}")
            return True


class PositionTracker:
    """
    Tracks open positions and enforces trading rules.
    Persists positions to DynamoDB for crash recovery.
    Uses separate tables for dry-run vs live mode to prevent conflicts.
    """
    
    def __init__(self, dry_run: bool = True, use_dynamodb: bool = True):
        self.positions: Dict[str, Position] = {}
        self.dry_run = dry_run
        self.use_dynamodb = use_dynamodb
        self._dynamodb_table = None
        self._table_name = POSITION_TABLE_DRYRUN if dry_run else POSITION_TABLE
        
        if use_dynamodb:
            self._load_positions_from_dynamodb()
    
    def _get_table(self):
        """Lazy-load DynamoDB table."""
        if self._dynamodb_table is None:
            import boto3
            dynamodb = boto3.resource('dynamodb')
            self._dynamodb_table = dynamodb.Table(self._table_name)
        return self._dynamodb_table
    
    def _load_positions_from_dynamodb(self):
        """Load existing positions from DynamoDB on startup."""
        try:
            table = self._get_table()
            response = table.scan(
                FilterExpression='begins_with(pk, :prefix)',
                ExpressionAttributeValues={':prefix': 'POS#'}
            )
            
            loaded = 0
            expired = 0
            for item in response.get('Items', []):
                pos = Position(
                    ticker=item['ticker'],
                    contracts=int(item['contracts']),
                    avg_price_cents=float(item['avg_price_cents']),
                    entry_edge=float(item['entry_edge']),
                    last_edge=float(item['last_edge']),
                    btc_price_at_entry=float(item['btc_price_at_entry']),
                    strike_price=float(item['strike_price']),
                    opened_at=item['opened_at'],
                    expiry_time=item.get('expiry_time', '2000-01-01T00:00:00')  # Default to expired
                )
                
                # Check if expired - don't load stale positions
                if pos.is_expired():
                    expired += 1
                    self._delete_position_from_dynamodb(pos.ticker)
                    print(f"  ⏰ Cleaned up expired position: {pos.ticker}")
                else:
                    self.positions[pos.ticker] = pos
                    loaded += 1
            
            if loaded > 0:
                print(f"[PositionTracker] Loaded {loaded} active positions from {self._table_name}")
            if expired > 0:
                print(f"[PositionTracker] Cleaned up {expired} expired positions")
                
        except Exception as e:
            error_msg = f"[PositionTracker] Could not load from DynamoDB ({self._table_name}): {e}"
            if self.dry_run:
                # In dry-run, warn but continue (table may not exist yet)
                print(f"[WARNING] {error_msg}")
            else:
                # In live mode, FAIL HARD - we could duplicate positions otherwise
                raise RuntimeError(f"CRITICAL: {error_msg}. Cannot safely trade without position state!")
    
    def _save_position_to_dynamodb(self, pos: Position) -> bool:
        """Save a position to DynamoDB. Returns True on success."""
        if not self.use_dynamodb:
            return True
        try:
            from decimal import Decimal
            table = self._get_table()
            table.put_item(Item={
                'pk': f'POS#{pos.ticker}',
                'sk': 'CURRENT',
                'ticker': pos.ticker,
                'contracts': pos.contracts,
                'avg_price_cents': Decimal(str(pos.avg_price_cents)),
                'entry_edge': Decimal(str(pos.entry_edge)),
                'last_edge': Decimal(str(pos.last_edge)),
                'btc_price_at_entry': Decimal(str(pos.btc_price_at_entry)),
                'strike_price': Decimal(str(pos.strike_price)),
                'opened_at': pos.opened_at,
                'expiry_time': pos.expiry_time
            })
            return True
        except Exception as e:
            error_msg = f"[PositionTracker] Failed to save to DynamoDB: {e}"
            if self.dry_run:
                print(f"[WARNING] {error_msg}")
                return True  # Continue in dry-run
            else:
                print(f"[CRITICAL] {error_msg}")
                return False  # Caller should NOT proceed with trade

    
    def _delete_position_from_dynamodb(self, ticker: str):
        """Delete a position from DynamoDB."""
        if not self.use_dynamodb:
            return
        try:
            table = self._get_table()
            table.delete_item(Key={'pk': f'POS#{ticker}', 'sk': 'CURRENT'})
        except Exception as e:
            print(f"[PositionTracker] Failed to delete from DynamoDB: {e}")
    
    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions
    
    def get_position(self, ticker: str) -> Optional[Position]:
        return self.positions.get(ticker)
    
    def can_add_to_position(self, ticker: str, current_edge: float) -> bool:
        """Check if we can add to an existing position (5pp rule)."""
        pos = self.positions.get(ticker)
        if not pos:
            return True  # No existing position, can open new
        
        edge_increase = current_edge - pos.last_edge
        if edge_increase >= EDGE_INCREASE_THRESHOLD:
            print(f"  ✅ Edge increased by {edge_increase:.1f}pp (>{EDGE_INCREASE_THRESHOLD}pp) - can add")
            return True
        else:
            print(f"  ⏸️ Edge increase {edge_increase:.1f}pp < {EDGE_INCREASE_THRESHOLD}pp - skip add")
            return False
    
    def open_position(self, ticker: str, contracts: int, price_cents: float,
                      edge: float, btc_price: float, strike_price: float,
                      expiry_time: str):
        """Open a new position."""
        pos = Position(
            ticker=ticker,
            contracts=contracts,
            avg_price_cents=price_cents,
            entry_edge=edge,
            last_edge=edge,
            btc_price_at_entry=btc_price,
            strike_price=strike_price,
            opened_at=datetime.utcnow().isoformat(),
            expiry_time=expiry_time
        )
        self.positions[ticker] = pos
        self._save_position_to_dynamodb(pos)
    
    def add_to_position(self, ticker: str, contracts: int, price_cents: float, edge: float):
        """Add contracts to an existing position."""
        pos = self.positions[ticker]
        
        # Calculate new weighted average price
        total_contracts = pos.contracts + contracts
        total_cost = pos.contracts * pos.avg_price_cents + contracts * price_cents
        new_avg = total_cost / total_contracts
        
        pos.contracts = total_contracts
        pos.avg_price_cents = new_avg
        pos.last_edge = edge
        self._save_position_to_dynamodb(pos)
    
    def update_edge(self, ticker: str, edge: float):
        """Update the current edge for a position."""
        if ticker in self.positions:
            self.positions[ticker].last_edge = edge
            # Don't save on every edge update to reduce DynamoDB writes
    
    def close_position(self, ticker: str) -> Optional[Position]:
        """Close and remove a position."""
        pos = self.positions.pop(ticker, None)
        if pos:
            self._delete_position_from_dynamodb(ticker)
        return pos
    
    def get_all_positions(self) -> List[Position]:
        return list(self.positions.values())
    
    def total_contracts(self) -> int:
        return sum(p.contracts for p in self.positions.values())


class HFTradingBot:
    """High-frequency BTC trading bot."""
    
    def __init__(self, dry_run: bool = True, refresh_interval: int = REFRESH_INTERVAL_SEC):
        self.dry_run = dry_run
        self.running = False
        self.refresh_interval = refresh_interval
        
        # Simulated balance for dry-run (tracks P&L)
        self._simulated_balance = DRY_RUN_STARTING_BALANCE
        
        # Position tracker - uses separate DynamoDB tables for dry-run vs live
        self.position_tracker = PositionTracker(dry_run=dry_run, use_dynamodb=True)
        
        # Performance tracker
        self.performance_tracker = PerformanceTracker(
            dry_run=dry_run,
            db_path="hf_trades.db"
        )
        
        # Kalshi client for live mode
        if not dry_run and KALSHI_AVAILABLE:
            self.kalshi = KalshiClient()
            # Sync with actual Kalshi positions on startup
            self._sync_kalshi_positions()
        else:
            self.kalshi = None
        
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
    
    def _sync_kalshi_positions(self):
        """Sync position tracker with actual Kalshi positions (live mode only)."""
        if not self.kalshi:
            return
        try:
            positions = self.kalshi.get_positions()
            kalshi_tickers = set()
            
            for pos in positions.get('market_positions', []):
                ticker = pos.get('ticker', '')
                if not ticker.startswith(BTC_SERIES):
                    continue
                    
                kalshi_tickers.add(ticker)
                contracts = pos.get('position', 0)
                
                if contracts > 0 and not self.position_tracker.has_position(ticker):
                    print(f"[SYNC] Found Kalshi position not in tracker: {ticker} ({contracts} contracts)")
                    # We don't have full info, but log it - could create Position with defaults
                    
            # Check for tracker positions not on Kalshi
            for ticker in list(self.position_tracker.positions.keys()):
                if ticker not in kalshi_tickers:
                    print(f"[SYNC] Tracker position not on Kalshi (settled?): {ticker}")
                    
        except Exception as e:
            print(f"[WARNING] Could not sync Kalshi positions: {e}")
    
    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully."""
        print("\n\n🛑 Shutdown signal received...")
        self.running = False


    
    def get_btc_price(self) -> Optional[float]:
        """
        Fetch current BTC price from Coinbase.
        Includes sanity checks to prevent trading on bad data.
        """
        try:
            url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                price = float(response.json()['data']['amount'])
                
                # CRITICAL SANITY CHECK: BTC price must be reasonable
                if price < 10000 or price > 500000:
                    print(f"🚨 CRITICAL ERROR: BTC price ${price:,.2f} is outside valid range ($10k-$500k)")
                    print(f"   This is likely a data error. REFUSING to trade.")
                    return None
                
                return price
                
        except Exception as e:
            print(f"[ERROR] Failed to get BTC price: {e}")
        return None


    
    def get_volatility(self) -> Optional[Dict]:
        """Fetch volatility from DynamoDB."""
        try:
            import boto3
            dynamodb = boto3.resource('dynamodb')
            table = dynamodb.Table(VOL_TABLE)
            
            response = table.get_item(Key={'pk': 'VOL', 'sk': 'LATEST'})
            item = response.get('Item')
            
            if item:
                return {
                    '15m_std': float(item.get('vol_15m_std', 0)),
                    '15m_samples': int(item.get('vol_15m_samples', 0)),
                }
        except Exception as e:
            print(f"[ERROR] Failed to get volatility: {e}")
        return None
    
    def get_et_time(self) -> datetime:
        """Get current Eastern Time using proper timezone handling."""
        utc_now = datetime.now(timezone.utc)
        et_tz = ZoneInfo("America/New_York")
        return utc_now.astimezone(et_tz)
    
    def get_next_hour_event_ticker(self) -> str:
        """Get the event ticker for the next hour's BTC contract."""
        et_time = self.get_et_time()
        next_hour = et_time + timedelta(hours=1)
        
        year = next_hour.strftime('%y')
        month = next_hour.strftime('%b').upper()
        day = next_hour.strftime('%d')
        hour = next_hour.strftime('%H')
        
        return f"{BTC_SERIES}-{year}{month}{day}{hour}"
    
    def get_markets(self, event_ticker: str) -> List[Dict]:
        """Fetch all markets for a BTC hourly event."""
        try:
            url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
            response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
            
            if response.status_code == 200:
                markets = response.json().get('markets', [])
                # Sort by floor_strike
                markets.sort(key=lambda x: x.get('floor_strike', 0) or 0)
                return markets
        except Exception as e:
            print(f"[ERROR] Failed to get markets: {e}")
        return []
    
    def calculate_model_probability(self, btc_price: float, strike_price: float,
                                     vol_std_pct: float, minutes_to_settlement: int) -> Optional[float]:
        """Calculate probability BTC stays below strike."""
        if vol_std_pct <= 0 or minutes_to_settlement <= 0:
            return None
        
        # Scale volatility to time remaining
        vol_scaled = vol_std_pct * math.sqrt(minutes_to_settlement / 15)
        
        # Distance in percent
        price_diff_pct = (strike_price - btc_price) / btc_price * 100
        std_devs_above = price_diff_pct / vol_scaled if vol_scaled > 0 else 0
        
        # Normal CDF approximation
        def norm_cdf(z):
            if z < -6:
                return 0.0
            if z > 6:
                return 1.0
            t = 1 / (1 + 0.2316419 * abs(z))
            d = 0.3989423 * math.exp(-z * z / 2)
            p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
            return 1 - p if z > 0 else p
        
        return norm_cdf(std_devs_above)
    
    def calculate_kalshi_fee_pct(self, price_cents: int) -> float:
        """
        Calculate Kalshi fee as a percentage of the contract cost.
        
        Kalshi charges: ceil(0.07 × contracts × price × (1 - price/100))
        This simplifies to approximately: 7% × (1 - price/100) as percentage of cost
        
        Examples:
            - At 80¢: ~1.4% fee
            - At 90¢: ~0.7% fee
            - At 50¢: ~3.5% fee
        """
        if price_cents <= 0 or price_cents >= 100:
            return 0.0
        
        # Fee as percentage of contract cost
        # fee = 0.07 * price * (1 - price/100) per contract
        # As % of cost: fee / price * 100 = 7 * (1 - price/100)
        fee_pct = KALSHI_FEE_RATE * (1 - price_cents / 100) * 100
        return fee_pct
    
    def calculate_net_edge(self, gross_edge_pct: float, price_cents: int) -> float:
        """
        Calculate net edge after accounting for Kalshi trading fees.
        
        Args:
            gross_edge_pct: Raw edge (model_prob - market_prob) * 100
            price_cents: The NO ask price in cents
            
        Returns:
            Net edge after fees (as percentage points)
        """
        fee_pct = self.calculate_kalshi_fee_pct(price_cents)
        net_edge = gross_edge_pct - fee_pct
        return net_edge

    
    def calculate_kelly_contracts(self, win_prob: float, no_price_cents: int, 
                                   bankroll: float) -> int:
        """Calculate number of contracts using Kelly criterion."""
        if no_price_cents <= 0 or no_price_cents >= 100:
            return 0
        
        # Odds ratio
        profit = 100 - no_price_cents
        risk = no_price_cents
        b = profit / risk
        
        p = win_prob
        q = 1 - p
        
        # Kelly fraction
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, min(kelly, KELLY_FRACTION))
        
        bet_amount = bankroll * kelly
        contracts = int(bet_amount / (no_price_cents / 100))
        
        return min(contracts, MAX_CONTRACTS)
    
    def get_account_balance(self) -> Optional[float]:
        """
        Get account balance.
        - Dry-run: Returns simulated balance (starts at $200, adjusts with trades)
        - Live: Fetches from Kalshi API, returns None on failure
        """
        if self.dry_run:
            return self._simulated_balance
        
        if self.kalshi:
            try:
                balance_data = self.kalshi.get_balance()
                balance = balance_data.get('balance', 0) / 100
                if balance <= 0:
                    print("[WARNING] Kalshi balance is $0 or negative!")
                return balance
            except Exception as e:
                print(f"[ERROR] Failed to get Kalshi balance: {e}")
                return None  # Caller should skip trading this cycle
        
        return None  # No Kalshi client in live mode = error
    
    def _update_simulated_balance(self, cost_cents: float, action: TradeAction):
        """Update simulated balance after a dry-run trade."""
        if not self.dry_run:
            return
        
        cost_dollars = cost_cents / 100
        if action in [TradeAction.OPEN, TradeAction.ADD]:
            # Buying: subtract cost
            self._simulated_balance -= cost_dollars
        elif action == TradeAction.LIQUIDATE:
            # Selling: we get back approximately what we paid (simplified)
            # In reality would depend on current price, but for simulation just reverse
            self._simulated_balance += cost_dollars

    
    def execute_trade(self, ticker: str, contracts: int, price: int,
                      action: TradeAction, btc_price: float, strike_price: float,
                      model_prob: float, edge: float) -> Optional[str]:
        """
        Execute a trade (or simulate in dry-run).
        Returns order_id on success, None on failure.
        Only returns success if order is actually FILLED.
        """
        
        market_prob = price / 100
        cost_cents = contracts * price
        
        # Record the trade
        trade = TradeRecord(
            timestamp=datetime.utcnow().isoformat(),
            ticker=ticker,
            action=action,
            side="NO",
            contracts=contracts,
            price_cents=price,
            edge_pct=edge,
            btc_price=btc_price,
            strike_price=strike_price,
            model_prob=model_prob,
            market_prob=market_prob
        )
        
        if self.dry_run:
            trade.order_id = f"DRY-{int(time.time()*1000)}"
            self.performance_tracker.record_trade(trade)
            # Update simulated balance
            self._update_simulated_balance(cost_cents, action)
            return trade.order_id
        
        # Live trade
        if self.kalshi:
            try:
                if action == TradeAction.LIQUIDATE:
                    # Exit: Sell NO contracts at market price
                    result = self.kalshi.sell_order(
                        ticker=ticker,
                        side="no",
                        count=contracts,
                        price=None  # Market order for immediate exit
                    )
                    order = result.get('order', {})
                    trade.order_id = order.get('order_id')
                    status = order.get('status', '')
                    
                    # For market orders, verify filled
                    if status != 'filled':
                        print(f"  🚨 CRITICAL: Liquidation order status '{status}' - NOT FILLED!")
                        print(f"     Order ID: {trade.order_id}")
                        print(f"     DO NOT close position tracker - manual intervention needed!")
                        return None  # Do NOT close position
                    
                    self.performance_tracker.record_trade(trade)
                    return trade.order_id
                    
                else:
                    # Buy order with limit price
                    result = self.kalshi.create_order(
                        ticker=ticker,
                        side="no",
                        count=contracts,
                        price=price
                    )
                    order = result.get('order', {})
                    trade.order_id = order.get('order_id')
                    status = order.get('status', '')
                    
                    if status == 'filled':
                        # Immediately filled - success
                        self.performance_tracker.record_trade(trade)
                        return trade.order_id
                    
                    elif status == 'resting':
                        # Order is on the book but not filled
                        # Wait briefly for fill, then cancel if not
                        print(f"  ⏳ Order resting... waiting up to {ORDER_TIMEOUT_SEC}s for fill")
                        
                        for _ in range(ORDER_TIMEOUT_SEC // 2):
                            time.sleep(2)
                            try:
                                order_status = self.kalshi.get_order(trade.order_id)
                                current_status = order_status.get('order', {}).get('status', '')
                                if current_status == 'filled':
                                    print(f"  ✅ Order filled!")
                                    self.performance_tracker.record_trade(trade)
                                    return trade.order_id
                                elif current_status not in ['resting', 'pending']:
                                    # Cancelled or rejected
                                    print(f"  ❌ Order status changed to: {current_status}")
                                    break
                            except:
                                pass
                        
                        # Timed out - cancel the order
                        print(f"  ⏰ Order timed out - cancelling")
                        try:
                            self.kalshi.cancel_order(trade.order_id)
                        except:
                            pass
                        return None
                    
                    else:
                        # Unknown status
                        print(f"  ⚠️ Unexpected order status: {status}")
                        return None
                        
            except Exception as e:
                print(f"[ERROR] Trade failed: {e}")
                return None
        
        return None

    
    def scan_and_trade(self):
        """Main trading logic - scan markets and execute trades."""
        et_time = self.get_et_time()
        minutes_to_hour = 60 - et_time.minute
        
        print(f"\n{'='*60}")
        print(f"🔍 Scan at {et_time.strftime('%H:%M:%S')} ET ({minutes_to_hour} min to settlement)")
        print(f"{'='*60}")
        
        # Get BTC price
        btc_price = self.get_btc_price()
        if not btc_price:
            print("[SKIP] Could not get BTC price")
            return
        print(f"📊 BTC: ${btc_price:,.2f}")
        
        # Get volatility
        vol_data = self.get_volatility()
        if not vol_data or vol_data['15m_samples'] < 10:
            print("[SKIP] Insufficient volatility data")
            return
        vol_15m = vol_data['15m_std']
        print(f"📈 Volatility (15m): {vol_15m:.4f}%")
        
        # Get markets
        event_ticker = self.get_next_hour_event_ticker()
        markets = self.get_markets(event_ticker)
        if not markets:
            print(f"[SKIP] No markets for {event_ticker}")
            return
        print(f"📋 {len(markets)} markets for {event_ticker}")
        
        # Calculate when this contract expires (top of next hour ET)
        next_hour_et = et_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        # Convert to UTC for storage (et_time is already timezone-aware)
        expiry_utc = next_hour_et.astimezone(timezone.utc)
        expiry_time = expiry_utc.isoformat()

        
        # Get bankroll - CRITICAL: Skip cycle if can't fetch
        bankroll = self.get_account_balance()
        if bankroll is None:
            print("[SKIP] Could not get account balance - cannot size positions safely")
            return
        print(f"💰 Bankroll: ${bankroll:.2f}")
        
        # Calculate current exposure
        current_exposure = sum(p.total_cost() for p in self.position_tracker.get_all_positions())
        max_allowed_exposure = bankroll * MAX_EXPOSURE_FRACTION
        remaining_exposure = max_allowed_exposure - current_exposure
        
        print(f"📊 Exposure: ${current_exposure:.2f} / ${max_allowed_exposure:.2f} ({current_exposure/bankroll*100:.1f}%)")
        
        if remaining_exposure <= 0:
            print(f"⚠️ MAX EXPOSURE REACHED - no new positions until exposure decreases")
        
        # Check trading cutoff - don't open new positions in last 15 minutes
        can_open_new = minutes_to_hour > TRADING_CUTOFF_MINUTES and remaining_exposure > 0
        if minutes_to_hour <= TRADING_CUTOFF_MINUTES:
            print(f"⏰ Trading cutoff: {minutes_to_hour} min remaining (< {TRADING_CUTOFF_MINUTES} min)")
            print("   Will only manage existing positions, no new entries")
        
        # Scan all strikes above current price
        for market in markets:
            strike = market.get('floor_strike')
            if not strike or strike <= btc_price:
                continue
            
            ticker = market.get('ticker')
            no_ask = market.get('no_ask', 0)
            no_bid = market.get('no_bid', 0)
            
            # Skip if no ask price available
            if not no_ask or no_ask <= 0 or no_ask >= 100:
                continue
            
            # Calculate spread for slippage check
            spread = (no_ask - no_bid) if no_bid > 0 else None
            
            # Calculate model probability
            model_prob = self.calculate_model_probability(
                btc_price, strike, vol_15m, minutes_to_hour
            )
            if model_prob is None:
                continue
            
            # Calculate edge (GROSS and NET)
            market_prob = no_ask / 100
            gross_edge = (model_prob - market_prob) * 100
            fee_pct = self.calculate_kalshi_fee_pct(no_ask)
            net_edge = gross_edge - fee_pct
            bps_above = (strike - btc_price) / btc_price * 10000
            
            # Update edge for existing positions (use net edge)
            self.position_tracker.update_edge(ticker, net_edge)
            
            # Record observation for price level analytics
            if gross_edge > 0:
                self.performance_tracker.record_observation(
                    ticker=ticker,
                    price_cents=no_ask,
                    edge_pct=net_edge,  # Record NET edge for accurate analytics
                    model_prob=model_prob,
                    market_prob=market_prob,
                    btc_price=btc_price,
                    strike_price=strike,
                    bps_above=bps_above,
                    minutes_to_settlement=minutes_to_hour,
                    was_traded=False,
                    bid_price_cents=no_bid if no_bid > 0 else None,
                    expiry_time=expiry_time
                )

            # Check if NET edge is profitable after fees
            if net_edge >= MIN_EDGE_PCT and can_open_new:
                bps_above = (strike - btc_price) / btc_price * 10000
                print(f"\n  🎯 {ticker}: Strike ${strike:,.0f} ({bps_above:.0f}bps above)")
                print(f"     Model: {model_prob*100:.1f}% | Market: {market_prob*100:.1f}%")
                print(f"     Gross edge: {gross_edge:.1f}% | Fee: {fee_pct:.1f}% | NET: {net_edge:.1f}%")

                
                # SLIPPAGE CHECK: Skip if spread is too wide
                if spread is not None and spread > MAX_SLIPPAGE_CENTS:
                    print(f"     ⚠️ SKIP: Spread {spread}¢ > max allowed {MAX_SLIPPAGE_CENTS}¢")
                    continue
                elif spread is not None:
                    print(f"     Spread: {spread}¢ ✓")
                
                if self.position_tracker.has_position(ticker):
                    # Check 5pp rule
                    if self.position_tracker.can_add_to_position(ticker, net_edge):
                        contracts = self.calculate_kelly_contracts(model_prob, no_ask, remaining_exposure)
                        if contracts > 0:
                            order_id = self.execute_trade(
                                ticker, contracts, no_ask, TradeAction.ADD,
                                btc_price, strike, model_prob, net_edge
                            )
                            if order_id:
                                self.position_tracker.add_to_position(ticker, contracts, no_ask, net_edge)
                                remaining_exposure -= contracts * no_ask / 100
                else:
                    # Open new position - use remaining exposure for sizing
                    contracts = self.calculate_kelly_contracts(model_prob, no_ask, remaining_exposure)
                    if contracts > 0:
                        order_id = self.execute_trade(
                            ticker, contracts, no_ask, TradeAction.OPEN,
                            btc_price, strike, model_prob, net_edge
                        )
                        if order_id:
                            self.position_tracker.open_position(
                                ticker, contracts, no_ask, net_edge, btc_price, strike,
                                expiry_time
                            )
                            remaining_exposure -= contracts * no_ask / 100

        
        # Check for exits - FIXED: iterate over COPY to avoid modifying during iteration
        for pos in list(self.position_tracker.get_all_positions()):
            if pos.last_edge <= EXIT_EDGE_PCT:
                print(f"\n  🔴 EXIT {pos.ticker}: edge dropped to {pos.last_edge:.1f}% (<= {EXIT_EDGE_PCT}%)")
                order_id = self.execute_trade(
                    pos.ticker, pos.contracts, int(pos.avg_price_cents),
                    TradeAction.LIQUIDATE, btc_price, pos.strike_price,
                    0, pos.last_edge
                )
                if order_id:
                    self.position_tracker.close_position(pos.ticker)
        
        # Summary
        positions = self.position_tracker.get_all_positions()
        if positions:
            total_exposure = sum(p.total_cost() for p in positions)
            print(f"\n📦 Open positions: {len(positions)}, Total contracts: {self.position_tracker.total_contracts()}")
            print(f"   Total exposure: ${total_exposure:.2f}")
            for p in positions:
                print(f"   {p.ticker}: {p.contracts} @ {p.avg_price_cents:.0f}¢ (edge: {p.last_edge:.1f}%)")

    
    def run(self):
        """Main loop - run until shutdown."""
        mode = "DRY-RUN 🧪" if self.dry_run else "LIVE 🔴"
        print(f"\n{'#'*70}")
        print(f"# BTC High-Frequency Trading Bot - {mode}")
        print(f"# Edge threshold: {MIN_EDGE_PCT}% | Exit at: {EXIT_EDGE_PCT}%")
        print(f"# Kelly: {KELLY_FRACTION*100}% | Max contracts/trade: {MAX_CONTRACTS}")
        print(f"# Max exposure: {MAX_EXPOSURE_FRACTION*100}% of bankroll")
        print(f"# Max slippage: {MAX_SLIPPAGE_CENTS}¢ spread")
        print(f"# Refresh: {self.refresh_interval}s | Cutoff: {TRADING_CUTOFF_MINUTES} min")
        if self.dry_run:
            print(f"# Starting balance: ${DRY_RUN_STARTING_BALANCE:.2f}")
        print(f"# Press Ctrl+C to stop")
        print(f"{'#'*70}\n")
        
        self.running = True

        
        try:
            while self.running:
                try:
                    self.scan_and_trade()
                    
                    # Check for expired contracts and update outcomes
                    btc_price = self.get_btc_price()
                    if btc_price and self.dry_run:
                        settled = self.performance_tracker.update_settlement_outcomes(btc_price)
                        if settled:
                            print(f"   📊 Updated {settled} contract settlements")
                            
                except Exception as e:
                    print(f"\n[ERROR] Scan failed: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Sleep in small increments to allow graceful shutdown
                for _ in range(self.refresh_interval):
                    if not self.running:
                        break
                    time.sleep(1)

        
        finally:
            print("\n🛑 Shutting down...")
            self.performance_tracker.print_summary()
            self.performance_tracker.save_session()
            print("👋 Goodbye!")


def main():
    parser = argparse.ArgumentParser(description='BTC High-Frequency Trading Bot')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Run in dry-run mode (no real trades)')
    parser.add_argument('--interval', type=int, default=REFRESH_INTERVAL_SEC,
                        help=f'Refresh interval in seconds (default: {REFRESH_INTERVAL_SEC})')
    
    args = parser.parse_args()
    
    bot = HFTradingBot(dry_run=args.dry_run, refresh_interval=args.interval)
    bot.run()


if __name__ == "__main__":
    main()
