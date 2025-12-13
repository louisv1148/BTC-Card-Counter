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
    cursor.execute("""
        SELECT ticker, contracts, price_cents, edge_pct, strike_price, timestamp
        FROM trades
        WHERE action = 'open'
        AND ticker NOT IN (SELECT ticker FROM trades WHERE action = 'liquidate')
        ORDER BY timestamp DESC
    """)
    
    # Fetch current market data for open positions
    year = next_hour.strftime('%y')
    month = next_hour.strftime('%b').upper()
    day = next_hour.strftime('%d')
    hour = next_hour.strftime('%H')
    event_ticker = f"KXBTCD-{year}{month}{day}{hour}"
    
    current_market_data = {}
    try:
        url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
        response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
        if response.status_code == 200:
            markets = response.json().get('markets', [])
            for market in markets:
                ticker = market.get('ticker')
                if ticker:
                    current_market_data[ticker] = {
                        'no_bid': market.get('no_bid', 0),
                        'no_ask': market.get('no_ask', 0)
                    }
    except:
        pass
    
    open_positions = []
    total_exposure = 0
    for row in cursor.fetchall():
        ticker, contracts, entry_price, edge, strike, ts = row
        exposure = contracts * entry_price / 100
        total_exposure += exposure
        
        # Get current market data
        current_data = current_market_data.get(ticker, {})
        current_bid = current_data.get('no_bid', 0)
        current_ask = current_data.get('no_ask', 0)
        
        # Calculate unrealized P&L
        cost = contracts * entry_price / 100
        entry_fee = calculate_kalshi_fee(contracts, entry_price)
        
        # Current value if we sold at bid
        if current_bid > 0:
            proceeds = contracts * current_bid / 100
            exit_fee = calculate_kalshi_fee(contracts, current_bid)
            unrealized_pnl = proceeds - cost - entry_fee - exit_fee
        else:
            unrealized_pnl = -cost - entry_fee  # Worst case
        
        open_positions.append({
            'ticker': ticker,
            'contracts': contracts,
            'price_cents': int(entry_price),
            'edge': edge,
            'strike': strike,
            'opened': datetime.fromisoformat(ts).strftime("%m/%d %H:%M"),
            'current_bid': current_bid,
            'current_ask': current_ask,
            'unrealized_pnl': unrealized_pnl
        })

    
    
    # Get fair values - fetch current Kalshi market data for all strikes
    import requests
    
    # Get next hour event ticker
    et_time = datetime.now()  # Simplified - should use ET timezone
    next_hour = et_time + timedelta(hours=1)
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
            
            # Get volatility for model calculation
            cursor.execute("SELECT vol_15m_std, vol_15m_samples FROM (SELECT * FROM price_observations ORDER BY timestamp DESC LIMIT 1)")
            vol_row = cursor.fetchone()
            vol_std = vol_row[0] if vol_row else 0.08
            
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
                
                fair_values.append({
                    'strike': strike,
                    'bps_above': bps_above,
                    'no_bid': no_bid,
                    'no_ask': no_ask,
                    'edge': net_edge,
                    'ticker': ticker
                })
            
            # Sort by strike
            fair_values.sort(key=lambda x: x['strike'])
            
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
    
    # Get closed trades
    cursor.execute("""
        SELECT ticker, contracts, price_cents, edge_pct, timestamp
        FROM trades
        WHERE action = 'liquidate'
        AND timestamp > datetime('now', '-1 hour')
        ORDER BY timestamp DESC
    """)
    
    closed_trades = []
    for row in cursor.fetchall():
        ticker, contracts, price, edge, ts = row
        closed_trades.append({
            'ticker': ticker,
            'contracts': contracts,
            'price': int(price),
            'edge': edge,
            'closed': datetime.fromisoformat(ts).strftime("%H:%M:%S")
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
    print("🔄 Starting status.json generator (Ctrl+C to stop)")
    while True:
        try:
            generate_status()
            time.sleep(10)  # Update every 10 seconds
        except KeyboardInterrupt:
            print("\n👋 Stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)
