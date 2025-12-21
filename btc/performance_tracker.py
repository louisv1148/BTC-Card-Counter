"""
Performance Tracker for BTC High-Frequency Trading Bot

Tracks all trades, calculates P&L, and provides session statistics.
Supports both dry-run (SQLite) and live (DynamoDB) modes.
"""

import json
import sqlite3
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict
from enum import Enum


class TradeAction(Enum):
    OPEN = "open"
    ADD = "add"
    LIQUIDATE = "liquidate"


@dataclass
class TradeRecord:
    """Record of a single trade action."""
    timestamp: str
    ticker: str
    action: TradeAction
    side: str  # "NO" always for this bot
    contracts: int
    price_cents: int
    edge_pct: float
    btc_price: float
    strike_price: float
    model_prob: float
    market_prob: float
    order_id: Optional[str] = None
    settlement_result: Optional[str] = None  # "win", "lose", or None if not settled
    realized_pnl: Optional[float] = None


@dataclass
class SessionStats:
    """Aggregated statistics for a trading session."""
    session_start: str
    session_end: str
    total_trades: int
    trades_opened: int
    trades_added: int
    trades_liquidated: int
    positions_settled: int
    wins: int
    losses: int
    win_rate: float
    total_contracts_traded: int
    total_cost: float
    total_realized_pnl: float
    avg_edge_at_entry: float
    avg_edge_accuracy: float  # How often model was right


class PerformanceTracker:
    """
    Tracks trading performance for both dry-run and live modes.
    
    Dry-run: Uses local SQLite database
    Live: Uses DynamoDB (extends BTCTradeLog table)
    """
    
    def __init__(self, dry_run: bool = True, db_path: str = "trades.db"):
        self.dry_run = dry_run
        self.db_path = db_path
        self.session_start = datetime.utcnow().isoformat()
        self.trades: List[TradeRecord] = []
        
        # Always initialize DynamoDB for Lambda access
        self._init_dynamodb()
        
        if dry_run:
            self._init_sqlite()

    
    def _init_sqlite(self):
        """Initialize SQLite database for dry-run mode."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                edge_pct REAL NOT NULL,
                btc_price REAL NOT NULL,
                strike_price REAL NOT NULL,
                model_prob REAL NOT NULL,
                market_prob REAL NOT NULL,
                order_id TEXT,
                settlement_result TEXT,
                realized_pnl REAL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                stats_json TEXT
            )
        """)
        
        # Price level observations - tracks ALL edge opportunities (traded or not)
        # Enhanced with outcome tracking and slippage data
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                edge_pct REAL NOT NULL,
                model_prob REAL NOT NULL,
                market_prob REAL NOT NULL,
                btc_price REAL NOT NULL,
                strike_price REAL NOT NULL,
                bps_above_current REAL NOT NULL,
                minutes_to_settlement INTEGER NOT NULL,
                was_traded INTEGER DEFAULT 0,
                -- Slippage tracking
                bid_price_cents INTEGER,
                spread_cents INTEGER,
                -- Settlement outcome tracking (updated after contract settles)
                actual_outcome TEXT,  -- 'NO_WIN', 'NO_LOSE', or NULL if not settled
                settlement_btc_price REAL,
                model_was_correct INTEGER  -- 1 if model prediction matched outcome
            )
        """)
        
        # Pending settlements - contracts we're tracking for outcome
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_settlements (
                ticker TEXT PRIMARY KEY,
                strike_price REAL NOT NULL,
                expiry_time TEXT NOT NULL,
                observation_count INTEGER DEFAULT 0
            )
        """)
        
        # Start new session
        cursor.execute(
            "INSERT INTO sessions (start_time) VALUES (?)",
            (self.session_start,)
        )
        self.session_id = cursor.lastrowid
        self.conn.commit()
        
        print(f"[PerformanceTracker] SQLite initialized: {self.db_path}")
    
    def _init_dynamodb(self):
        """Initialize DynamoDB connection for trade logging."""
        import boto3
        self.dynamodb = boto3.resource('dynamodb')
        # Use same table as position tracker for consistency
        table_name = 'BTCHFPositions-DryRun' if self.dry_run else 'BTCTradeLog'
        self.table = self.dynamodb.Table(table_name)
        print(f"[PerformanceTracker] DynamoDB initialized: {table_name}")

    
    def record_trade(self, trade: TradeRecord):
        """Record a trade to the appropriate storage."""
        self.trades.append(trade)
        
        # Always write to DynamoDB for Lambda access
        self._record_dynamodb(trade)
        
        # Also write to SQLite in dry-run mode for local analysis
        if self.dry_run:
            self._record_sqlite(trade)

        
        action_emoji = {
            TradeAction.OPEN: "🟢",
            TradeAction.ADD: "➕",
            TradeAction.LIQUIDATE: "🔴"
        }
        
        emoji = action_emoji.get(trade.action, "📝")
        print(f"{emoji} [{trade.action.value.upper()}] {trade.ticker}: "
              f"{trade.contracts} contracts @ {trade.price_cents}¢ "
              f"(edge: {trade.edge_pct:.1f}%)")
    
    def _record_sqlite(self, trade: TradeRecord):
        """Record trade to SQLite."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trades (
                timestamp, ticker, action, side, contracts, price_cents,
                edge_pct, btc_price, strike_price, model_prob, market_prob,
                order_id, settlement_result, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.timestamp,
            trade.ticker,
            trade.action.value,
            trade.side,
            trade.contracts,
            trade.price_cents,
            trade.edge_pct,
            trade.btc_price,
            trade.strike_price,
            trade.model_prob,
            trade.market_prob,
            trade.order_id,
            trade.settlement_result,
            trade.realized_pnl
        ))
        self.conn.commit()
    
    def _record_dynamodb(self, trade: TradeRecord):
        """Record trade to DynamoDB."""
        item = {
            'pk': 'HF_TRADE',
            'sk': trade.timestamp,
            'ticker': trade.ticker,
            'action': trade.action.value,
            'side': trade.side,
            'contracts': trade.contracts,
            'price_cents': trade.price_cents,
            'edge_pct': Decimal(str(trade.edge_pct)),
            'btc_price': Decimal(str(trade.btc_price)),
            'strike_price': Decimal(str(trade.strike_price)),
            'model_prob': Decimal(str(trade.model_prob)),
            'market_prob': Decimal(str(trade.market_prob)),
        }
        if trade.order_id:
            item['order_id'] = trade.order_id
        if trade.settlement_result:
            item['settlement_result'] = trade.settlement_result
        if trade.realized_pnl is not None:
            item['realized_pnl'] = Decimal(str(trade.realized_pnl))
        
        self.table.put_item(Item=item)
    
    def update_settlement(self, ticker: str, result: str, realized_pnl: float):
        """
        Update a trade with settlement information.
        
        Args:
            ticker: Contract ticker
            result: "win" or "lose"
            realized_pnl: Actual profit/loss in dollars
        """
        for trade in reversed(self.trades):
            if trade.ticker == ticker and trade.settlement_result is None:
                trade.settlement_result = result
                trade.realized_pnl = realized_pnl
                
                if self.dry_run:
                    cursor = self.conn.cursor()
                    cursor.execute("""
                        UPDATE trades 
                        SET settlement_result = ?, realized_pnl = ?
                        WHERE ticker = ? AND settlement_result IS NULL
                    """, (result, realized_pnl, ticker))
                    self.conn.commit()
                
                print(f"💰 Settlement: {ticker} -> {result.upper()} "
                      f"(P&L: ${realized_pnl:+.2f})")
                break
    
    def get_session_stats(self) -> SessionStats:
        """Calculate and return current session statistics."""
        if not self.trades:
            return SessionStats(
                session_start=self.session_start,
                session_end=datetime.utcnow().isoformat(),
                total_trades=0,
                trades_opened=0,
                trades_added=0,
                trades_liquidated=0,
                positions_settled=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                total_contracts_traded=0,
                total_cost=0.0,
                total_realized_pnl=0.0,
                avg_edge_at_entry=0.0,
                avg_edge_accuracy=0.0
            )
        
        opened = [t for t in self.trades if t.action == TradeAction.OPEN]
        added = [t for t in self.trades if t.action == TradeAction.ADD]
        liquidated = [t for t in self.trades if t.action == TradeAction.LIQUIDATE]
        settled = [t for t in self.trades if t.settlement_result is not None]
        wins = [t for t in settled if t.settlement_result == "win"]
        losses = [t for t in settled if t.settlement_result == "lose"]
        
        total_contracts = sum(t.contracts for t in self.trades)
        total_cost = sum(t.contracts * t.price_cents / 100 for t in self.trades 
                        if t.action in [TradeAction.OPEN, TradeAction.ADD])
        total_pnl = sum(t.realized_pnl or 0 for t in settled)
        
        entry_edges = [t.edge_pct for t in opened]
        avg_edge = sum(entry_edges) / len(entry_edges) if entry_edges else 0
        
        # Edge accuracy: did the model predict correctly?
        correct_predictions = len(wins)
        edge_accuracy = correct_predictions / len(settled) if settled else 0
        
        return SessionStats(
            session_start=self.session_start,
            session_end=datetime.utcnow().isoformat(),
            total_trades=len(self.trades),
            trades_opened=len(opened),
            trades_added=len(added),
            trades_liquidated=len(liquidated),
            positions_settled=len(settled),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / len(settled) if settled else 0,
            total_contracts_traded=total_contracts,
            total_cost=total_cost,
            total_realized_pnl=total_pnl,
            avg_edge_at_entry=avg_edge,
            avg_edge_accuracy=edge_accuracy
        )
    
    def print_summary(self):
        """Print a formatted summary of the session."""
        stats = self.get_session_stats()
        
        print("\n" + "="*60)
        print("📊 SESSION SUMMARY")
        print("="*60)
        print(f"Duration: {stats.session_start} → {stats.session_end}")
        print(f"\n📈 Trade Activity:")
        print(f"   Total trades: {stats.total_trades}")
        print(f"   Positions opened: {stats.trades_opened}")
        print(f"   Positions added to: {stats.trades_added}")
        print(f"   Positions liquidated: {stats.trades_liquidated}")
        print(f"   Total contracts: {stats.total_contracts_traded}")
        print(f"\n💵 Financial:")
        print(f"   Total cost: ${stats.total_cost:.2f}")
        print(f"   Realized P&L: ${stats.total_realized_pnl:+.2f}")
        print(f"\n🎯 Performance:")
        print(f"   Positions settled: {stats.positions_settled}")
        print(f"   Wins: {stats.wins} | Losses: {stats.losses}")
        print(f"   Win rate: {stats.win_rate*100:.1f}%")
        print(f"   Avg edge at entry: {stats.avg_edge_at_entry:.1f}%")
        print(f"   Edge accuracy: {stats.avg_edge_accuracy*100:.1f}%")
        
        # Print price level analysis if available
        if self.dry_run:
            self.print_price_analysis()
        
        print("="*60 + "\n")
    
    def record_observation(self, ticker: str, price_cents: int, edge_pct: float,
                           model_prob: float, market_prob: float, btc_price: float,
                           strike_price: float, bps_above: float, minutes_to_settlement: int,
                           was_traded: bool = False, bid_price_cents: int = None,
                           expiry_time: str = None):
        """
        Record a price observation for analytics.
        Tracks ALL edge opportunities, not just traded ones.
        
        Args:
            bid_price_cents: The bid price (for slippage analysis)
            expiry_time: ISO format expiry time (for settlement outcome tracking)
        """
        if not self.dry_run:
            return  # Only track in dry-run mode
        
        # Calculate spread if bid is available
        spread_cents = None
        if bid_price_cents is not None:
            spread_cents = price_cents - bid_price_cents
        
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO price_observations (
                timestamp, ticker, price_cents, edge_pct, model_prob, market_prob,
                btc_price, strike_price, bps_above_current, minutes_to_settlement, 
                was_traded, bid_price_cents, spread_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            ticker,
            price_cents,
            edge_pct,
            model_prob,
            market_prob,
            btc_price,
            strike_price,
            bps_above,
            minutes_to_settlement,
            1 if was_traded else 0,
            bid_price_cents,
            spread_cents
        ))
        
        # Track contract for settlement outcome if expiry provided
        if expiry_time:
            cursor.execute("""
                INSERT OR REPLACE INTO pending_settlements (ticker, strike_price, expiry_time, observation_count)
                VALUES (?, ?, ?, 
                    COALESCE((SELECT observation_count + 1 FROM pending_settlements WHERE ticker = ?), 1)
                )
            """, (ticker, strike_price, expiry_time, ticker))
        
        self.conn.commit()
    
    def update_settlement_outcomes(self, final_btc_price: float):
        """
        Update all pending settlements with actual outcomes.
        Call this periodically to check for expired contracts.
        
        Args:
            final_btc_price: The BTC price at settlement time
        """
        if not self.dry_run:
            return
        
        cursor = self.conn.cursor()
        now = datetime.utcnow().isoformat()
        
        # Find expired contracts
        cursor.execute("""
            SELECT ticker, strike_price, expiry_time 
            FROM pending_settlements 
            WHERE expiry_time < ?
        """, (now,))
        
        expired = cursor.fetchall()
        
        for ticker, strike_price, expiry_time in expired:
            # Determine outcome: NO wins if BTC stayed BELOW strike
            no_wins = final_btc_price < strike_price
            outcome = 'NO_WIN' if no_wins else 'NO_LOSE'
            
            # Update all observations for this ticker
            cursor.execute("""
                UPDATE price_observations 
                SET actual_outcome = ?,
                    settlement_btc_price = ?,
                    model_was_correct = CASE 
                        WHEN model_prob > 0.5 AND ? = 'NO_WIN' THEN 1
                        WHEN model_prob <= 0.5 AND ? = 'NO_LOSE' THEN 1
                        ELSE 0
                    END
                WHERE ticker = ?
            """, (outcome, final_btc_price, outcome, outcome, ticker))
            
            # Remove from pending
            cursor.execute("DELETE FROM pending_settlements WHERE ticker = ?", (ticker,))
            
            print(f"📊 Settlement: {ticker} → {outcome} (BTC: ${final_btc_price:,.2f}, Strike: ${strike_price:,.0f})")
        
        self.conn.commit()
        return len(expired)

    
    def print_price_analysis(self):
        """Print analysis of edge opportunities by price level."""
        if not self.dry_run:
            return
        
        cursor = self.conn.cursor()
        
        # Check if we have observations
        cursor.execute("SELECT COUNT(*) FROM price_observations")
        count = cursor.fetchone()[0]
        if count == 0:
            return
        
        print(f"\n📊 Price Level Analysis ({count} observations):")
        
        # Edge by price bucket (10¢ buckets)
        cursor.execute("""
            SELECT 
                (price_cents / 10) * 10 as bucket,
                COUNT(*) as observations,
                AVG(edge_pct) as avg_edge,
                MAX(edge_pct) as max_edge,
                SUM(was_traded) as trades,
                AVG(bps_above_current) as avg_bps
            FROM price_observations
            WHERE edge_pct > 0
            GROUP BY bucket
            ORDER BY bucket
        """)
        
        rows = cursor.fetchall()
        if rows:
            print("\n   Price Bucket | Obs | Avg Edge | Max Edge | Trades | Avg BPS")
            print("   " + "-"*60)
            for row in rows:
                bucket_start = row[0]
                bucket_end = bucket_start + 9
                print(f"   {bucket_start:3d}-{bucket_end:2d}¢      | {row[1]:3d} | {row[2]:7.1f}% | {row[3]:7.1f}% | {row[4]:6d} | {row[5]:6.0f}")
        
        # ============ NEW: Outcome Analysis by Price Band ============
        cursor.execute("""
            SELECT 
                (price_cents / 10) * 10 as bucket,
                COUNT(*) as total,
                SUM(CASE WHEN actual_outcome = 'NO_WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN actual_outcome = 'NO_LOSE' THEN 1 ELSE 0 END) as losses,
                AVG(edge_pct) as avg_edge,
                AVG(model_was_correct) * 100 as accuracy_pct
            FROM price_observations
            WHERE actual_outcome IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
        """)
        
        rows = cursor.fetchall()
        if rows:
            print("\n   🎯 PRICE BAND OUTCOMES (where does edge break down?):")
            print("   Price Bucket | Settled | Wins | Losses | Win Rate | Avg Edge | Model Accuracy")
            print("   " + "-"*75)
            for row in rows:
                bucket_start = row[0]
                bucket_end = bucket_start + 9
                total = row[1]
                wins = row[2] or 0
                losses = row[3] or 0
                win_rate = (wins / total * 100) if total > 0 else 0
                avg_edge = row[4] or 0
                accuracy = row[5] or 0
                
                # Flag price bands where edge is NOT working
                flag = "⚠️" if win_rate < 60 else "✅" if win_rate >= 80 else "  "
                print(f"   {bucket_start:3d}-{bucket_end:2d}¢      | {total:7d} | {wins:4d} | {losses:6d} | {win_rate:7.1f}% | {avg_edge:7.1f}% | {accuracy:6.1f}% {flag}")
        
        # ============ NEW: Slippage Analysis ============
        cursor.execute("""
            SELECT 
                AVG(spread_cents) as avg_spread,
                MAX(spread_cents) as max_spread,
                MIN(spread_cents) as min_spread,
                COUNT(*) as obs_with_spread
            FROM price_observations
            WHERE spread_cents IS NOT NULL
        """)
        
        row = cursor.fetchone()
        if row and row[3] > 0:
            print(f"\n   📉 SLIPPAGE ANALYSIS ({row[3]} observations with bid/ask data):")
            print(f"      Average spread: {row[0]:.1f}¢")
            print(f"      Max spread:     {row[1]}¢")
            print(f"      Min spread:     {row[2]}¢")
            
            # Spread impact by price level
            cursor.execute("""
                SELECT 
                    (price_cents / 10) * 10 as bucket,
                    AVG(spread_cents) as avg_spread,
                    AVG(spread_cents * 100.0 / price_cents) as spread_pct
                FROM price_observations
                WHERE spread_cents IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket
            """)
            
            rows = cursor.fetchall()
            if rows:
                print("\n      Price Bucket | Avg Spread | Spread %")
                print("      " + "-"*40)
                for row in rows:
                    bucket_start = row[0]
                    bucket_end = bucket_start + 9
                    print(f"      {bucket_start:3d}-{bucket_end:2d}¢      | {row[1]:9.1f}¢ | {row[2]:7.2f}%")
        
        # High-edge opportunities by time to settlement
        cursor.execute("""
            SELECT 
                minutes_to_settlement,
                COUNT(*) as opportunities,
                AVG(edge_pct) as avg_edge
            FROM price_observations
            WHERE edge_pct >= 10
            GROUP BY minutes_to_settlement
            ORDER BY minutes_to_settlement DESC
        """)
        
        rows = cursor.fetchall()
        if rows:
            print("\n   ⏱️  TIME TO SETTLEMENT (10%+ edge opportunities):")
            print("   Min to Settlement | 10%+ Edge Opps | Avg Edge")
            print("   " + "-"*45)
            for row in rows[:10]:  # Top 10
                print(f"   {row[0]:18d} | {row[1]:14d} | {row[2]:7.1f}%")

    
    def save_session(self):
        """Save session stats and close connections."""
        stats = self.get_session_stats()
        
        if self.dry_run:
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE sessions 
                SET end_time = ?, stats_json = ?
                WHERE id = ?
            """, (
                stats.session_end,
                json.dumps(asdict(stats)),
                self.session_id
            ))
            self.conn.commit()
            self.conn.close()
            print(f"[PerformanceTracker] Session saved to {self.db_path}")
        
        return stats


# For testing
if __name__ == "__main__":
    tracker = PerformanceTracker(dry_run=True, db_path="test_trades.db")
    
    # Simulate some trades
    trade1 = TradeRecord(
        timestamp=datetime.utcnow().isoformat(),
        ticker="KXBTCD-25DEC1220-T90500",
        action=TradeAction.OPEN,
        side="NO",
        contracts=3,
        price_cents=85,
        edge_pct=12.5,
        btc_price=90255.0,
        strike_price=90500.0,
        model_prob=0.975,
        market_prob=0.85
    )
    tracker.record_trade(trade1)
    
    trade2 = TradeRecord(
        timestamp=datetime.utcnow().isoformat(),
        ticker="KXBTCD-25DEC1220-T90500",
        action=TradeAction.ADD,
        side="NO",
        contracts=2,
        price_cents=82,
        edge_pct=18.0,
        btc_price=90200.0,
        strike_price=90500.0,
        model_prob=0.98,
        market_prob=0.82
    )
    tracker.record_trade(trade2)
    
    # Simulate settlement
    tracker.update_settlement("KXBTCD-25DEC1220-T90500", "win", 0.75)
    
    tracker.print_summary()
    tracker.save_session()
    
    # Cleanup test file
    os.remove("test_trades.db")
    print("Test completed successfully!")
