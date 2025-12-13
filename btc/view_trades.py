#!/usr/bin/env python3
"""
View trading history from SQLite database
"""
import sqlite3
import sys
from datetime import datetime

DB_PATH = "hf_trades.db"

def view_trades(limit=50):
    """View recent trades"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT timestamp, ticker, action, contracts, price_cents, edge_pct, 
               btc_price, strike_price
        FROM trades 
        ORDER BY timestamp DESC 
        LIMIT ?
    """, (limit,))
    
    print(f"\n{'='*100}")
    print(f"RECENT TRADES (last {limit})")
    print(f"{'='*100}")
    print(f"{'Time':<20} {'Ticker':<25} {'Action':<10} {'Qty':<5} {'Price':<7} {'Edge':<7} {'BTC $':<10} {'Strike $':<10}")
    print("-" * 100)
    
    for row in cursor.fetchall():
        ts, ticker, action, contracts, price, edge, btc, strike = row
        dt = datetime.fromisoformat(ts).strftime("%m/%d %H:%M:%S")
        print(f"{dt:<20} {ticker:<25} {action:<10} {contracts:<5} {price:<7.0f}¢ {edge:<6.1f}% ${btc:<9,.0f} ${strike:<9,.0f}")
    
    conn.close()

def view_sessions():
    """View session summaries"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT started_at, ended_at, total_trades, total_contracts, 
               avg_edge, max_edge, total_pnl
        FROM sessions 
        ORDER BY started_at DESC 
        LIMIT 10
    """)
    
    print(f"\n{'='*100}")
    print(f"SESSION HISTORY")
    print(f"{'='*100}")
    print(f"{'Started':<20} {'Ended':<20} {'Trades':<8} {'Contracts':<10} {'Avg Edge':<10} {'Max Edge':<10} {'P&L':<10}")
    print("-" * 100)
    
    for row in cursor.fetchall():
        start, end, trades, contracts, avg_edge, max_edge, pnl = row
        start_dt = datetime.fromisoformat(start).strftime("%m/%d %H:%M")
        end_dt = datetime.fromisoformat(end).strftime("%m/%d %H:%M") if end else "Running"
        pnl_str = f"${pnl:.2f}" if pnl else "N/A"
        print(f"{start_dt:<20} {end_dt:<20} {trades:<8} {contracts:<10} {avg_edge:<9.1f}% {max_edge:<9.1f}% {pnl_str:<10}")
    
    conn.close()

def view_price_outcomes():
    """View price band outcomes"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            (price_cents / 10) * 10 as bucket,
            COUNT(*) as total,
            SUM(CASE WHEN actual_outcome = 'NO_WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN actual_outcome = 'NO_LOSE' THEN 1 ELSE 0 END) as losses,
            AVG(edge_pct) as avg_edge
        FROM price_observations
        WHERE actual_outcome IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """)
    
    rows = cursor.fetchall()
    if not rows:
        print("\nNo settled observations yet")
        conn.close()
        return
    
    print(f"\n{'='*80}")
    print(f"PRICE BAND OUTCOMES")
    print(f"{'='*80}")
    print(f"{'Bucket':<15} {'Total':<8} {'Wins':<8} {'Losses':<8} {'Win %':<10} {'Avg Edge':<10}")
    print("-" * 80)
    
    for bucket, total, wins, losses, avg_edge in rows:
        win_pct = (wins / total * 100) if total > 0 else 0
        flag = "✅" if win_pct >= 80 else "⚠️" if win_pct < 60 else ""
        print(f"{int(bucket):3d}-{int(bucket)+9:2d}¢       {total:<8} {wins:<8} {losses:<8} {win_pct:<9.1f}% {avg_edge:<9.1f}% {flag}")
    
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "sessions":
            view_sessions()
        elif sys.argv[1] == "outcomes":
            view_price_outcomes()
        elif sys.argv[1] == "all":
            view_sessions()
            view_trades()
            view_price_outcomes()
        else:
            print("Usage: python view_trades.py [sessions|outcomes|all]")
    else:
        view_trades()
