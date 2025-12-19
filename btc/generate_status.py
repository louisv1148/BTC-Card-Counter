#!/usr/bin/env python3
"""
Generate static status.json file for CloudFront deployment
Run this periodically (e.g., every 10 seconds) to update the dashboard
"""
import json
import sqlite3
import math
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = "hf_trades.db"
OUTPUT_FILE = "status.json"

def calculate_kalshi_fee_pct(price_cents):
    """Calculate fee as percentage of cost"""
    if price_cents <= 0 or price_cents >= 100:
        return 0.0
    price = price_cents / 100
    return 0.07 * (1 - price) * 100

def calculate_kalshi_fee(contracts, price_cents):
    """
    Calculate exact Kalshi fee using their formula:
    fee_cents = ceil(0.07 × contracts × price × (1 - price/100))
    Returns fee in dollars
    """
    price = price_cents / 100
    fee_cents = math.ceil(0.07 * contracts * price_cents * (1 - price))
    return fee_cents / 100

def generate_status():
    """Generate status JSON file"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get latest BTC price
    cursor.execute("SELECT btc_price FROM price_observations ORDER BY timestamp DESC LIMIT 1")
    row = cursor.fetchone()
    btc_price = row[0] if row else 0
    
    # Calculate settlement time using proper ET timezone
    utc_now = datetime.now(timezone.utc)
    et_tz = ZoneInfo("America/New_York")
    et_time = utc_now.astimezone(et_tz)
    next_hour = (et_time + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minutes_to_settlement = int((next_hour - et_time).total_seconds() / 60)

    
    # Get open positions with current market data
    # Need to handle same ticker being traded multiple times
    # An open is "still open" if there's no liquidate with higher ID for same ticker
    cursor.execute("""
        SELECT t1.id, t1.ticker, t1.contracts, t1.price_cents, t1.edge_pct, t1.strike_price, t1.timestamp
        FROM trades t1
        WHERE t1.action = 'open'
        AND NOT EXISTS (
            SELECT 1 FROM trades t2 
            WHERE t2.ticker = t1.ticker 
            AND t2.action = 'liquidate' 
            AND t2.id > t1.id
        )
        ORDER BY t1.timestamp DESC
    """)

    
    # Fetch current market data for open positions
    # Need to fetch from EACH position's specific event, not just current hour
    current_market_data = {}
    events_fetched = set()
    
    # First pass: identify all events we need to fetch
    rows = cursor.fetchall()
    for row in rows:
        ticker = row[1]  # row[0] is ID, row[1] is ticker

        # Extract event ticker from position ticker (e.g., KXBTCD-25DEC1410-T89249.99 -> KXBTCD-25DEC1410)
        parts = ticker.rsplit('-', 1)
        if len(parts) >= 1:
            event_ticker = parts[0]
            if event_ticker not in events_fetched:
                events_fetched.add(event_ticker)
                try:
                    url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
                    response = requests.get(url, headers={'Accept': 'application/json'}, timeout=5)
                    if response.status_code == 200:
                        markets = response.json().get('markets', [])
                        for market in markets:
                            mkt_ticker = market.get('ticker')
                            if mkt_ticker:
                                current_market_data[mkt_ticker] = {
                                    'no_bid': market.get('no_bid', 0),
                                    'no_ask': market.get('no_ask', 0),
                                    'status': market.get('status', 'unknown')
                                }
                except:
                    pass    
    open_positions = []
    total_exposure = 0
    for row in rows:  # Use rows we already fetched
        trade_id, ticker, contracts, entry_price, edge, strike, ts = row

        exposure = contracts * entry_price / 100
        total_exposure += exposure
        
        # Get current market data
        current_data = current_market_data.get(ticker, {})
        current_bid = current_data.get('no_bid', 0)
        current_ask = current_data.get('no_ask', 0)
        
        # Calculate unrealized P&L
        cost = contracts * entry_price / 100
        entry_fee = calculate_kalshi_fee(contracts, entry_price)
        
        # Determine if position is expired (no bid, ask is 100 = settled)
        market_status = current_data.get('status', 'unknown')
        is_expired = current_bid == 0 and current_ask >= 99
        
        # Current value if we sold at bid
        if current_bid > 0:
            proceeds = contracts * current_bid / 100
            exit_fee = calculate_kalshi_fee(contracts, current_bid)
            unrealized_pnl = proceeds - cost - entry_fee - exit_fee
        elif is_expired:
            # Expired - NO likely won (settled at 100¢ ask means NO is worth $1)
            # We get $1 per contract
            proceeds = contracts * 1.00
            exit_fee = 0  # No exit fee on settlement
            unrealized_pnl = proceeds - cost - entry_fee
        else:
            unrealized_pnl = -cost - entry_fee  # Worst case
        
        # Calculate percentage gain/loss
        pnl_pct = (unrealized_pnl / cost * 100) if cost > 0 else 0
        
        open_positions.append({
            'ticker': ticker,
            'contracts': contracts,
            'price_cents': int(entry_price),
            'edge': edge,
            'strike': strike,
            'opened': datetime.fromisoformat(ts).strftime("%m/%d %H:%M"),
            'current_bid': current_bid,
            'current_ask': current_ask,
            'unrealized_pnl': unrealized_pnl,
            'pnl_pct': pnl_pct,
            'status': 'EXPIRED' if is_expired else 'ACTIVE'
        })



    
    # Get fair values - fetch current Kalshi market data for all strikes
    # Use the next_hour already calculated with ET timezone
    year = next_hour.strftime('%y')
    month = next_hour.strftime('%b').upper()
    day = next_hour.strftime('%d')
    hour = next_hour.strftime('%H')
    event_ticker = f"KXBTCD-{year}{month}{day}{hour}"

    
    # Fetch markets from Kalshi
    fair_values = []
    try:
        url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
        response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
        
        if response.status_code == 200:
            markets = response.json().get('markets', [])
            
            # Use default volatility for model calculation (simplified for dashboard)
            vol_std = 0.02  # 2% default volatility

            
            # Calculate minutes to settlement
            minutes_to_settlement = 60 - et_time.minute
            
            for market in markets:
                strike = market.get('floor_strike')
                if not strike or strike <= btc_price:
                    continue
                    
                ticker = market.get('ticker')
                no_bid = market.get('no_bid', 0)
                no_ask = market.get('no_ask', 0)
                
                if not no_ask or no_ask <= 0:
                    continue
                
                # Calculate model probability (same as bot logic)
                vol_scaled = vol_std * (minutes_to_settlement / 15) ** 0.5
                price_diff_pct = (strike - btc_price) / btc_price * 100
                std_devs_above = price_diff_pct / vol_scaled if vol_scaled > 0 else 0
                
                # Normal CDF approximation
                def norm_cdf(z):
                    if z < -6: return 0.0
                    if z > 6: return 1.0
                    t = 1 / (1 + 0.2316419 * abs(z))
                    d = 0.3989423 * math.exp(-z * z / 2)
                    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
                    return 1 - p if z > 0 else p
                
                model_prob = norm_cdf(std_devs_above)
                market_prob = no_ask / 100
                gross_edge = (model_prob - market_prob) * 100
                
                # Calculate net edge (after fees)
                fee_pct = calculate_kalshi_fee_pct(no_ask)
                net_edge = gross_edge - fee_pct
                
                # Calculate bps above current price
                bps_above = (strike - btc_price) / btc_price * 10000
                
                # Model fair value in cents (what model thinks NO is worth)
                model_fair_cents = int(model_prob * 100)
                
                fair_values.append({
                    'strike': strike,
                    'bps_above': bps_above,
                    'no_bid': no_bid,
                    'no_ask': no_ask,
                    'model_fair': model_fair_cents,  # What model thinks NO is worth
                    'edge': net_edge,
                    'ticker': ticker
                })

            
            # Sort by strike and limit to top 5 (closest to current price)
            fair_values.sort(key=lambda x: x['strike'])
            fair_values = fair_values[:5]

            
    except Exception as e:
        print(f"Error fetching fair values: {e}")
    

    # Get hourly summary
    cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            AVG(edge_pct) as avg_edge
        FROM trades
        WHERE timestamp > datetime('now', '-1 hour')
    """)
    
    row = cursor.fetchone()
    hourly_summary = {
        'trades': row[0] if row else 0,
        'avg_edge': row[1] if row and row[1] else 0
    }
    
    # Get closed trades with entry price and P&L
    cursor.execute("""
        SELECT 
            t_close.ticker, 
            t_close.contracts, 
            t_open.price_cents as entry_price,
            t_close.price_cents as exit_price, 
            t_close.timestamp,
            t_close.realized_pnl
        FROM trades t_close
        JOIN trades t_open ON t_open.ticker = t_close.ticker 
            AND t_open.action = 'open'
            AND t_open.id < t_close.id
            AND NOT EXISTS (
                SELECT 1 FROM trades t_mid 
                WHERE t_mid.ticker = t_close.ticker 
                AND t_mid.action = 'liquidate'
                AND t_mid.id > t_open.id 
                AND t_mid.id < t_close.id
            )
        WHERE t_close.action = 'liquidate'
        AND t_close.timestamp > datetime('now', '-1 hour')
        ORDER BY t_close.timestamp DESC
    """)
    
    closed_trades = []
    for row in cursor.fetchall():
        ticker, contracts, entry_price, exit_price, ts, realized_pnl = row
        # Calculate P&L if not stored
        entry_cost = contracts * entry_price / 100
        if realized_pnl is None:
            proceeds = contracts * exit_price / 100
            entry_fee = calculate_kalshi_fee(contracts, entry_price)
            exit_fee = calculate_kalshi_fee(contracts, exit_price)
            realized_pnl = proceeds - entry_cost - entry_fee - exit_fee
        # Calculate percentage gain/loss
        pnl_pct = (realized_pnl / entry_cost * 100) if entry_cost > 0 else 0
        # All timestamps are stored in UTC - convert to ET for display
        closed_dt = datetime.fromisoformat(ts.replace('+00:00', '').replace('Z', ''))
        # Treat as UTC and convert to ET
        closed_dt = closed_dt.replace(tzinfo=timezone.utc).astimezone(et_tz)
        closed_trades.append({
            'ticker': ticker,
            'contracts': contracts,
            'entry_price': int(entry_price),
            'exit_price': int(exit_price),
            'pnl': realized_pnl,
            'pnl_pct': pnl_pct,
            'closed': closed_dt.strftime("%H:%M:%S")
        })






    
    # Calculate P&L (for dry-run mode)
    # Get all closed positions (opened then liquidated)
    cursor.execute("""
        SELECT 
            t_open.id,
            t_open.ticker,
            t_open.contracts,
            t_open.price_cents as entry_price,
            t_close.price_cents as exit_price
        FROM trades t_open
        JOIN trades t_close ON t_open.ticker = t_close.ticker 
            AND t_close.action = 'liquidate'
            AND t_close.id > t_open.id
        WHERE t_open.action = 'open'
        GROUP BY t_open.id
    """)
    
    realized_pnl = 0
    closed_count = 0
    total_fees_paid = 0
    
    for row in cursor.fetchall():
        open_id, ticker, contracts, entry_price, exit_price = row
        
        # Cost to enter
        cost = contracts * entry_price / 100
        entry_fee = calculate_kalshi_fee(contracts, entry_price)
        
        # Proceeds from exit
        proceeds = contracts * exit_price / 100
        exit_fee = calculate_kalshi_fee(contracts, exit_price)
        
        # Net P&L = proceeds - cost - fees
        position_pnl = proceeds - cost - entry_fee - exit_fee
        realized_pnl += position_pnl
        total_fees_paid += entry_fee + exit_fee
        closed_count += 1
    
    # Unrealized P&L from open positions
    unrealized_pnl = sum(pos['unrealized_pnl'] for pos in open_positions)
    
    # Total P&L
    total_pnl = realized_pnl + unrealized_pnl
    
    pnl_data = {
        'realized': realized_pnl,
        'unrealized': unrealized_pnl,
        'total': total_pnl,
        'closed_trades': closed_count,
        'total_fees': total_fees_paid
    }


    
    conn.close()

    
    data = {
        'btc_price': btc_price,
        'settlement_time': next_hour.strftime("%I:%M %p ET"),
        'minutes_to_settlement': minutes_to_settlement,
        'open_positions': open_positions,
        'total_exposure': total_exposure,
        'fair_values': fair_values,
        'hourly_summary': hourly_summary,
        'closed_trades': closed_trades,
        'pnl': pnl_data,
        'last_updated': datetime.now().isoformat()
    }


    
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    
    print(f"✅ Generated {OUTPUT_FILE} at {datetime.now().strftime('%H:%M:%S')}")

if __name__ == '__main__':
    import time
    import subprocess
    
    S3_BUCKET = "btc-trading-dashboard-1765598917"
    
    print("🔄 Starting status.json generator with S3 upload (Ctrl+C to stop)")    # Track last time we checked for expired positions (don't spam Kalshi API)
    last_expired_check = 0
    EXPIRED_CHECK_INTERVAL = 60  # Check every 60 seconds
    
    while True:
        try:
            # Periodically close expired positions (uses Kalshi API)
            current_time = time.time()
            if current_time - last_expired_check >= EXPIRED_CHECK_INTERVAL:
                try:
                    from close_expired import close_expired_positions
                    count, pnl = close_expired_positions(dry_run=False)
                    if count > 0:
                        print(f"  📦 Closed {count} expired positions for ${pnl:.2f}")
                    last_expired_check = current_time
                except Exception as e:
                    print(f"  ⚠️ Error closing expired: {e}")
                    last_expired_check = current_time
            
            generate_status()
            # Upload to S3
            result = subprocess.run(
                ['aws', 's3', 'cp', OUTPUT_FILE, f's3://{S3_BUCKET}/status.json', '--quiet'],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"  ⚠️ S3 upload failed: {result.stderr}")
            time.sleep(10)  # Update every 10 seconds
        except KeyboardInterrupt:
            print("\n👋 Stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

