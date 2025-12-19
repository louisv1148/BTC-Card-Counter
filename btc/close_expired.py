#!/usr/bin/env python3
"""
Close out expired positions in the trading database.

This script:
1. Finds all open positions (opened but never liquidated)
2. Checks if their contracts have expired (hour has passed)
3. Fetches actual settlement results from Kalshi API
4. Adds liquidate records with correct P&L based on whether NO won or lost
"""
import sqlite3
import re
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = "hf_trades.db"

def parse_ticker_hour(ticker: str) -> datetime:
    """
    Extract the settlement hour from a ticker.
    e.g., KXBTCD-25DEC1410-T89249.99 -> 2025-12-25 14:10 ET
    """
    # Pattern: KXBTCD-YYMmmDDHH-T...
    match = re.match(r'KXBTCD-(\d{2})([A-Z]{3})(\d{2})(\d{2})', ticker)
    if not match:
        return None
    
    year = 2000 + int(match.group(1))
    month_str = match.group(2)
    day = int(match.group(3))
    hour = int(match.group(4))
    
    months = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
              'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
    month = months.get(month_str, 1)
    
    et_tz = ZoneInfo("America/New_York")
    return datetime(year, month, day, hour, 0, 0, tzinfo=et_tz)


def get_event_ticker(ticker: str) -> str:
    """Extract event ticker from position ticker (e.g., KXBTCD-25DEC1817-T85749.99 -> KXBTCD-25DEC1817)"""
    parts = ticker.rsplit('-', 1)
    return parts[0] if len(parts) >= 1 else None


def fetch_settlement_results(event_ticker: str) -> dict:
    """
    Fetch settlement results for an event from Kalshi API.
    Returns dict mapping strike -> result ('yes' or 'no')
    """
    results = {}
    try:
        resp = requests.get(
            f'https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}',
            timeout=10
        )
        if resp.status_code == 200:
            markets = resp.json().get('markets', [])
            for m in markets:
                strike = m.get('floor_strike')
                result = m.get('result')  # 'yes', 'no', or None if not settled
                status = m.get('status')  # 'determined' if settled
                if strike and result:
                    results[strike] = result
    except Exception as e:
        print(f"  ⚠️ Could not fetch settlement for {event_ticker}: {e}")
    return results


def close_expired_positions(dry_run: bool = True):
    """
    Find and close all expired positions with correct settlement values.
    
    Args:
        dry_run: If True, just print what would be done. If False, actually update DB.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get current ET time
    utc_now = datetime.now(timezone.utc)
    et_tz = ZoneInfo("America/New_York")
    et_now = utc_now.astimezone(et_tz)
    
    print(f"Current time: {et_now.strftime('%Y-%m-%d %H:%M ET')}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE - MAKING CHANGES'}")
    print("-" * 60)
    
    # Find open positions (opened but not liquidated)
    # Use ID comparison to handle same ticker traded multiple times
    cursor.execute("""
        SELECT t1.id, t1.ticker, t1.contracts, t1.price_cents, t1.strike_price, t1.timestamp
        FROM trades t1
        WHERE t1.action = 'open'
        AND NOT EXISTS (
            SELECT 1 FROM trades t2 
            WHERE t2.ticker = t1.ticker 
            AND t2.action = 'liquidate' 
            AND t2.id > t1.id
        )
        ORDER BY t1.timestamp
    """)

    
    open_positions = cursor.fetchall()
    print(f"Found {len(open_positions)} open positions")
    
    # Group positions by event ticker to batch API calls
    events_to_check = set()
    for row in open_positions:
        ticker = row[1]
        event_ticker = get_event_ticker(ticker)
        settlement_time = parse_ticker_hour(ticker)
        if settlement_time and et_now >= settlement_time:
            events_to_check.add(event_ticker)
    
    # Fetch settlement results for all expired events
    settlement_cache = {}
    for event in events_to_check:
        settlement_cache[event] = fetch_settlement_results(event)
    
    closed_count = 0
    total_pnl = 0
    
    for row in open_positions:
        trade_id, ticker, contracts, entry_price, strike, opened_ts = row
        
        # Parse settlement hour from ticker
        settlement_time = parse_ticker_hour(ticker)
        if not settlement_time:
            print(f"  ⚠️  Could not parse ticker: {ticker}")
            continue
        
        # Check if expired (current time past settlement hour)
        if et_now < settlement_time:
            print(f"  ⏰ ACTIVE: {ticker} settles at {settlement_time.strftime('%H:%M ET')}")
            continue
        
        # Get actual settlement result from Kalshi
        event_ticker = get_event_ticker(ticker)
        results = settlement_cache.get(event_ticker, {})
        
        # Find result for this strike
        result = results.get(strike)
        if not result:
            # Try to find close match (floating point issues)
            for s, r in results.items():
                if abs(s - strike) < 1:
                    result = r
                    break
        
        if not result:
            print(f"  ⚠️ No settlement result for {ticker} (strike {strike})")
            continue
        
        # Determine payout based on result
        # We bet NO, so:
        #   - If result='no', NO won, we get $1 per contract (100¢)
        #   - If result='yes', YES won, NO lost, we get $0
        if result == 'no':
            exit_price = 100  # NO won - we get $1
            settlement_result = 'NO_WIN'
        else:
            exit_price = 0    # YES won - we get $0
            settlement_result = 'YES_WIN'
        
        # Calculate P&L
        entry_cost = contracts * entry_price / 100
        proceeds = contracts * exit_price / 100
        pnl = proceeds - entry_cost
        
        status_emoji = "✅" if pnl >= 0 else "❌"
        print(f"  {status_emoji} EXPIRED: {ticker}")
        print(f"     Entry: {entry_price}¢ → Exit: {exit_price}¢ | P&L: ${pnl:.2f} ({settlement_result})")
        
        if not dry_run:
            # Insert liquidate record with UTC timestamp for consistency
            cursor.execute("""
                INSERT INTO trades (timestamp, ticker, action, side, contracts, price_cents,
                                   edge_pct, btc_price, strike_price, model_prob, market_prob,
                                   order_id, settlement_result, realized_pnl)
                VALUES (?, ?, 'liquidate', 'NO', ?, ?, 0, 0, ?, 0, 0, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),

                ticker,
                contracts,
                exit_price,
                strike,
                f"SETTLEMENT-{trade_id}",
                settlement_result,
                pnl
            ))

        
        closed_count += 1
        total_pnl += pnl
    
    print("-" * 60)
    print(f"Positions to close: {closed_count}")
    print(f"Total P&L: ${total_pnl:.2f}")
    
    if not dry_run and closed_count > 0:
        conn.commit()
        print("✅ Changes committed to database")
    
    conn.close()
    return closed_count, total_pnl


if __name__ == "__main__":
    import sys
    
    print("=" * 60)
    print("CLOSE EXPIRED POSITIONS SCRIPT")
    print("=" * 60)
    
    # First do a dry run
    print("\n📋 DRY RUN (showing what would be done):\n")
    count, pnl = close_expired_positions(dry_run=True)
    
    if count > 0:
        print(f"\n{'=' * 60}")
        response = input(f"\nClose {count} positions for ${pnl:.2f} P&L? (yes/no): ")
        if response.lower() == 'yes':
            print("\n🔴 EXECUTING CHANGES:\n")
            close_expired_positions(dry_run=False)
            print("\n✅ Done!")
        else:
            print("Cancelled.")
    else:
        print("\nNo expired positions to close.")
