#!/usr/bin/env python3
"""
Real-time trading status dashboard
Shows: current BTC price, open positions, recent opportunities, and hourly summary
"""
import sqlite3
import sys
from datetime import datetime, timedelta

DB_PATH = "hf_trades.db"

def get_status():
    """Display comprehensive trading status"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get latest BTC price from observations
    cursor.execute("SELECT btc_price FROM price_observations ORDER BY timestamp DESC LIMIT 1")
    row = cursor.fetchone()
    btc_price = row[0] if row else 0
    
    # Get current hour's settlement time
    now = datetime.utcnow()
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minutes_to_settlement = int((next_hour - now).total_seconds() / 60)
    
    print(f"\n{'='*90}")
    print(f"🤖 BTC TRADING BOT STATUS - {datetime.now().strftime('%I:%M:%S %p')}")
    print(f"{'='*90}")
    print(f"💰 BTC/USD: ${btc_price:,.2f}")
    print(f"⏰ Settlement: {next_hour.strftime('%I:%M %p')} ET ({minutes_to_settlement} min)")
    print(f"{'='*90}\n")
    
    # OPEN POSITIONS
    cursor.execute("""
        SELECT ticker, contracts, price_cents, edge_pct, btc_price, strike_price, timestamp
        FROM trades
        WHERE action = 'open'
        AND ticker NOT IN (
            SELECT ticker FROM trades WHERE action = 'liquidate'
        )
        ORDER BY timestamp DESC
    """)
    
    positions = cursor.fetchall()
    if positions:
        print(f"📦 OPEN POSITIONS ({len(positions)})")
        print(f"{'-'*90}")
        print(f"{'Ticker':<30} {'Qty':<5} {'Entry':<8} {'Edge':<8} {'Strike':<12} {'Opened':<20}")
        print(f"{'-'*90}")
        
        total_exposure = 0
        for ticker, contracts, price, edge, btc, strike, ts in positions:
            opened = datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
            exposure = contracts * price / 100
            total_exposure += exposure
            print(f"{ticker:<30} {contracts:<5} {price:<7.0f}¢ {edge:<7.1f}% ${strike:<11,.0f} {opened:<20}")
        
        print(f"{'-'*90}")
        print(f"Total Exposure: ${total_exposure:.2f}\n")
    else:
        print("📦 OPEN POSITIONS: None\n")
    
    # RECENT OPPORTUNITIES (last 10 min)
    cursor.execute("""
        SELECT ticker, price_cents, edge_pct, strike_price, timestamp
        FROM price_observations
        WHERE edge_pct >= 10
        AND timestamp > datetime('now', '-10 minutes')
        ORDER BY timestamp DESC
        LIMIT 10
    """)
    
    opportunities = cursor.fetchall()
    if opportunities:
        print(f"🎯 RECENT OPPORTUNITIES (10%+ edge, last 10 min)")
        print(f"{'-'*90}")
        print(f"{'Ticker':<30} {'Price':<8} {'Net Edge':<10} {'Strike':<12} {'Time':<15}")
        print(f"{'-'*90}")
        
        for ticker, price, edge, strike, ts in opportunities:
            time_str = datetime.fromisoformat(ts).strftime("%H:%M:%S")
            print(f"{ticker:<30} {price:<7.0f}¢ {edge:<9.1f}% ${strike:<11,.0f} {time_str:<15}")
        print()
    else:
        print("🎯 RECENT OPPORTUNITIES: None (10%+ edge)\n")
    
    # HOURLY SUMMARY
    cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            SUM(contracts) as total_contracts,
            AVG(edge_pct) as avg_edge,
            MAX(edge_pct) as max_edge
        FROM trades
        WHERE timestamp > datetime('now', '-1 hour')
    """)
    
    row = cursor.fetchone()
    if row and row[0] > 0:
        trades, contracts, avg_edge, max_edge = row
        print(f"📊 LAST HOUR SUMMARY")
        print(f"{'-'*90}")
        print(f"Trades: {trades} | Contracts: {contracts} | Avg Edge: {avg_edge:.1f}% | Max Edge: {max_edge:.1f}%")
        print()
    
    # CLOSED TRADES (last hour)
    cursor.execute("""
        SELECT ticker, contracts, price_cents, edge_pct, timestamp
        FROM trades
        WHERE action = 'liquidate'
        AND timestamp > datetime('now', '-1 hour')
        ORDER BY timestamp DESC
    """)
    
    closed = cursor.fetchall()
    if closed:
        print(f"🔴 CLOSED TRADES (last hour)")
        print(f"{'-'*90}")
        print(f"{'Ticker':<30} {'Qty':<5} {'Exit':<8} {'Edge':<8} {'Closed':<15}")
        print(f"{'-'*90}")
        
        for ticker, contracts, price, edge, ts in closed:
            time_str = datetime.fromisoformat(ts).strftime("%H:%M:%S")
            print(f"{ticker:<30} {contracts:<5} {price:<7.0f}¢ {edge:<7.1f}% {time_str:<15}")
        print()
    
    conn.close()

if __name__ == "__main__":
    try:
        get_status()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
