"""
Dashboard Status Generator Lambda
Generates status.json and uploads to S3 for the dashboard.
Replaces the local generate_status.py script.
"""
import json
import math
import os
import boto3
import requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Environment-driven configuration (for dry-run vs live)
S3_BUCKET = os.environ.get('S3_BUCKET', 'btc-trading-dashboard-1765598917')
S3_KEY = os.environ.get('S3_KEY', 'status.json')
DYNAMODB_POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'BTCHFPositions-DryRun')
DYNAMODB_VOL_TABLE = os.environ.get('VOL_TABLE', 'BTCPriceHistory')
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
KALSHI_FEE_RATE = 0.07
STARTING_BALANCE = float(os.environ.get('STARTING_BALANCE', '200.0'))

# Initialize AWS clients
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')


def get_real_kalshi_balance():
    """Get actual balance from Kalshi API."""
    try:
        from kalshi_client import KalshiClient
        client = KalshiClient()
        balance_data = client.get_balance()
        # Kalshi returns balance in cents, convert to dollars
        return float(balance_data.get('balance', 0)) / 100
    except Exception as e:
        print(f"Error getting Kalshi balance: {e}")
        return 0.0


def get_real_kalshi_positions():
    """Get actual positions from Kalshi API."""
    try:
        from kalshi_client import KalshiClient
        client = KalshiClient()
        positions_data = client.get_positions()
        positions = []
        for pos in positions_data.get('market_positions', []):
            if pos.get('position', 0) != 0:  # Only include non-zero positions
                positions.append({
                    'ticker': pos.get('ticker', ''),
                    'contracts': abs(pos.get('position', 0)),
                    'avg_price_cents': pos.get('last_bought_price', 0),
                    'strike_price': 0,  # Would need to parse from ticker
                    'last_edge': 0,
                    'cost_basis': abs(pos.get('position', 0)) * pos.get('last_bought_price', 0) / 100
                })
        return positions
    except Exception as e:
        print(f"Error getting Kalshi positions: {e}")
        return []



def get_et_time():
    """Get current Eastern Time."""
    from zoneinfo import ZoneInfo
    utc_now = datetime.now(timezone.utc)
    et_tz = ZoneInfo("America/New_York")
    return utc_now.astimezone(et_tz)


def get_volatility():
    """Fetch volatility from DynamoDB."""
    try:
        table = dynamodb.Table(DYNAMODB_VOL_TABLE)
        response = table.get_item(Key={'pk': 'VOL', 'sk': 'LATEST'})
        item = response.get('Item')
        if item:
            return float(item.get('vol_15m_std', 0.02))
    except Exception as e:
        print(f"Error getting volatility: {e}")
    return 0.02


def get_recent_prices(minutes):
    """Get prices from the last N minutes from DynamoDB."""
    import boto3.dynamodb.conditions as conditions
    
    table = dynamodb.Table(DYNAMODB_VOL_TABLE)
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=minutes)
    
    prices = []
    
    # Query today's prices
    today_pk = f"PRICE#{now.strftime('%Y%m%d')}"
    today_start_sk = start_time.strftime('%H:%M:%S') if start_time.date() == now.date() else "00:00:00"
    
    print(f"[VOL DEBUG] Querying {minutes}m of prices: pk={today_pk}, sk >= {today_start_sk}")
    
    try:
        response = table.query(
            KeyConditionExpression=conditions.Key('pk').eq(today_pk) & 
                                  conditions.Key('sk').gte(today_start_sk)
        )
        
        print(f"[VOL DEBUG] Query returned {len(response.get('Items', []))} items")
        
        for item in response.get('Items', []):
            prices.append({
                'timestamp': item['timestamp_utc'],
                'price': float(item['price'])
            })
        
        # If we need data from yesterday (e.g., it's 00:05 and we need 60 min of data)
        if start_time.date() < now.date():
            yesterday_pk = f"PRICE#{start_time.strftime('%Y%m%d')}"
            yesterday_start_sk = start_time.strftime('%H:%M:%S')
            
            response = table.query(
                KeyConditionExpression=conditions.Key('pk').eq(yesterday_pk) & 
                                      conditions.Key('sk').gte(yesterday_start_sk)
            )
            
            print(f"[VOL DEBUG] Yesterday query returned {len(response.get('Items', []))} items")
            
            for item in response.get('Items', []):
                prices.append({
                    'timestamp': item['timestamp_utc'],
                    'price': float(item['price'])
                })
        
        # Sort by timestamp
        prices.sort(key=lambda x: x['timestamp'])
        
    except Exception as e:
        print(f"Error getting recent prices: {e}")
    
    print(f"[VOL DEBUG] Total prices found: {len(prices)}")
    return prices


def get_volatility_by_window():
    """Calculate volatility (std dev of returns) for each window from 2 to 60 minutes."""
    import statistics
    import math
    
    # Fetch last 60 minutes of prices
    prices = get_recent_prices(60)
    
    if len(prices) < 2:
        return []
    
    volatility_data = []
    
    for window in range(2, 61):
        # Take the last 'window' prices
        if len(prices) >= window:
            window_prices = prices[-window:]
        else:
            window_prices = prices
        
        if len(window_prices) >= 2:
            # Calculate minute-to-minute returns (percent)
            returns = []
            for i in range(1, len(window_prices)):
                ret = (window_prices[i]['price'] - window_prices[i-1]['price']) / window_prices[i-1]['price'] * 100
                returns.append(ret)
            
            # Calculate std dev and scale by sqrt(window) for time-scaled 1σ
            if len(returns) > 1:
                std_dev = statistics.stdev(returns)
                # Scale by sqrt of time period for proper volatility scaling
                scaled_vol = std_dev * math.sqrt(window)
            else:
                scaled_vol = 0
            
            volatility_data.append({
                'window': window,
                'volatility': round(scaled_vol, 4)
            })
    
    return volatility_data


def get_btc_price():
    """Fetch current BTC price from Coinbase."""
    try:
        response = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5
        )
        if response.status_code == 200:
            return float(response.json()['data']['amount'])
    except Exception as e:
        print(f"Error getting BTC price: {e}")
    return 0


def get_open_positions(current_event_prefix):
    """Get open positions from DynamoDB."""
    positions = []
    try:
        table = dynamodb.Table(DYNAMODB_POSITIONS_TABLE)
        response = table.scan(
            FilterExpression='begins_with(pk, :prefix)',
            ExpressionAttributeValues={':prefix': 'POS#'}
        )
        for item in response.get('Items', []):
            ticker = item.get('ticker', '')
            if ticker.startswith(current_event_prefix):
                positions.append({
                    'ticker': ticker,
                    'contracts': int(item.get('contracts', 0)),
                    'price_cents': float(item.get('avg_price_cents', 0)),
                    'last_edge': float(item.get('last_edge', 0)),
                    'strike_price': float(item.get('strike_price', 0)),
                    'opened_at': item.get('opened_at', '')
                })
    except Exception as e:
        print(f"Error getting positions: {e}")
    return positions


def get_trade_history():
    """Get all trade history from DynamoDB for P&L calculation."""
    closed_trades = []
    total_pnl = 0.0
    total_fees = 0.0
    
    try:
        table = dynamodb.Table(DYNAMODB_POSITIONS_TABLE)
        response = table.scan(
            FilterExpression='pk = :pk',
            ExpressionAttributeValues={':pk': 'HF_TRADE'}
        )
        
        # Sort all trades chronologically
        all_trades = sorted(response.get('Items', []), key=lambda x: x.get('sk', ''))
        
        # Track open positions by ticker to match with liquidates
        open_positions = {}  # ticker -> [list of opens in order]
        
        for item in all_trades:
            ticker = item.get('ticker', '')
            action = item.get('action', '')
            contracts = int(item.get('contracts', 0))
            price_cents = int(item.get('price_cents', 0))
            timestamp = item.get('sk', '')
            
            if action in ['open', 'add']:
                # Add to open positions for this ticker
                if ticker not in open_positions:
                    open_positions[ticker] = []
                open_positions[ticker].append({
                    'contracts': contracts,
                    'price_cents': price_cents,
                    'timestamp': timestamp,
                    'opened_at': timestamp,  # Track when position was opened
                    'model_fair': float(item.get('model_fair', 0) or 0),
                    'edge': float(item.get('edge_pct', 0) or 0),
                    'vol_std': float(item.get('vol_std', 0) or 0)
                })
            
            elif action in ['liquidate', 'expired_win']:
                # Match with the most recent open for this ticker
                entry_price = 0
                opened_at = ''
                open_model_fair = 0
                open_edge = 0
                open_vol_std = 0
                if ticker in open_positions and open_positions[ticker]:
                    # Use the first open (FIFO) and remove it
                    entry = open_positions[ticker].pop(0)
                    entry_price = entry['price_cents']
                    opened_at = entry.get('opened_at', '')
                    open_model_fair = entry.get('model_fair', 0)
                    open_edge = entry.get('edge', 0)
                    open_vol_std = entry.get('vol_std', 0)
                    
                    # If we used all opens, clear the list
                    if not open_positions[ticker]:
                        del open_positions[ticker]
                
                # Calculate P&L
                entry_cost = contracts * entry_price / 100
                exit_value = contracts * price_cents / 100
                entry_fee = calculate_kalshi_fee(contracts, entry_price) if entry_price > 0 else 0
                exit_fee = calculate_kalshi_fee(contracts, price_cents)
                pnl = exit_value - entry_cost - entry_fee - exit_fee
                pnl_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0
                
                total_pnl += pnl
                total_fees += entry_fee + exit_fee
                
                # Convert UTC timestamp to Central Time for display
                closed_time_display = timestamp
                timestamp_sort = timestamp  # Keep full timestamp for sorting
                try:
                    from zoneinfo import ZoneInfo
                    utc_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    ct_dt = utc_dt.astimezone(ZoneInfo('America/Mexico_City'))
                    closed_time_display = ct_dt.strftime('%H:%M:%S')
                    timestamp_sort = ct_dt.isoformat()  # Full ISO for sorting
                except:
                    closed_time_display = timestamp.split('T')[1][:8] if 'T' in timestamp else timestamp
                
                # Convert opened_at to Central Time for display
                opened_time_display = opened_at
                try:
                    from zoneinfo import ZoneInfo
                    if opened_at:
                        utc_dt = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
                        ct_dt = utc_dt.astimezone(ZoneInfo('America/Mexico_City'))
                        opened_time_display = ct_dt.strftime('%H:%M:%S')
                except:
                    opened_time_display = opened_at.split('T')[1][:8] if 'T' in opened_at else opened_at
                
                closed_trades.append({
                    'ticker': ticker,
                    'contracts': contracts,
                    'entry_price': entry_price,
                    'exit_price': price_cents,
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 1),
                    'opened': opened_time_display,
                    'closed': closed_time_display,
                    'timestamp_sort': timestamp_sort,
                    'model_fair': round(open_model_fair, 1),
                    'open_edge': round(open_edge, 1),
                    'vol_std': round(open_vol_std * 100, 2) if open_vol_std else 0  # Convert to percentage
                })
    
    except Exception as e:
        print(f"Error getting trade history: {e}")
    
    # Sort by full timestamp, then remove the sort key before returning
    sorted_trades = sorted(closed_trades, key=lambda x: x.get('timestamp_sort', ''), reverse=True)[:20]
    for trade in sorted_trades:
        trade.pop('timestamp_sort', None)
    
    return {
        'closed_trades': sorted_trades,
        'total_pnl': total_pnl,
        'total_fees': total_fees,
        'trade_count': len(closed_trades)
    }


def calculate_kalshi_fee(contracts, price_cents):
    """Calculate Kalshi fee."""
    price = price_cents / 100
    fee_cents = math.ceil(KALSHI_FEE_RATE * contracts * price_cents * (1 - price))
    return fee_cents / 100


def norm_cdf(z):
    """Normal CDF approximation."""
    if z < -6: return 0.0
    if z > 6: return 1.0
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p


def calculate_model_fair(btc_price, strike, vol_std, minutes_left):
    """
    Calculate model fair value for NO contract.
    
    vol_std is the ALREADY SCALED volatility (per-minute stdev × sqrt(window)).
    This represents the expected 1σ move over the time window.
    """
    if minutes_left <= 0 or vol_std <= 0:
        return 100 if btc_price < strike else 0
    
    # vol_std is already scaled, use directly
    price_diff_pct = (strike - btc_price) / btc_price * 100 if btc_price > 0 else 0
    std_devs = price_diff_pct / vol_std if vol_std > 0 else 0
    prob = norm_cdf(std_devs)
    return int(prob * 100)


def get_fair_values(btc_price, event_ticker, vol_std, minutes_left):
    """Get fair values for all strikes above current price."""
    fair_values = []
    try:
        url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
        print(f"[DEBUG] Fetching fair values from: {url}")
        response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
        print(f"[DEBUG] API status: {response.status_code}")
        
        if response.status_code == 200:
            markets = response.json().get('markets', [])
            print(f"[DEBUG] Markets found: {len(markets)}, BTC price: ${btc_price:,.2f}")
            
            strikes_above = 0
            for market in markets:
                strike = market.get('floor_strike')
                if not strike or strike <= btc_price:
                    continue
                
                strikes_above += 1
                no_bid = market.get('no_bid', 0)
                no_ask = market.get('no_ask', 0)
                
                if not no_ask or no_ask <= 0:
                    continue
                
                model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_left)
                bps_above = (strike - btc_price) / btc_price * 10000
                
                market_prob = no_ask / 100
                model_prob = model_fair / 100
                edge = (model_prob - market_prob) * 100
                
                fair_values.append({
                    'strike': strike + 0.01,  # Add $0.01 to match Kalshi display
                    'no_bid': no_bid,
                    'no_ask': no_ask,
                    'model_fair': model_fair,
                    'bps_above': bps_above,
                    'edge': edge
                })
            print(f"[DEBUG] Strikes above BTC: {strikes_above}, with valid asks: {len(fair_values)}")
    except Exception as e:
        print(f"Error getting fair values: {e}")
    
    return sorted(fair_values, key=lambda x: x['strike'])[:10]



def get_market_data(tickers):
    """Get current market data for tickers."""
    market_data = {}
    events_fetched = set()
    
    for ticker in tickers:
        parts = ticker.rsplit('-', 1)
        if len(parts) >= 1:
            event_ticker = parts[0]
            if event_ticker not in events_fetched:
                events_fetched.add(event_ticker)
                try:
                    url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
                    response = requests.get(url, headers={'Accept': 'application/json'}, timeout=5)
                    if response.status_code == 200:
                        for market in response.json().get('markets', []):
                            mkt_ticker = market.get('ticker')
                            if mkt_ticker:
                                market_data[mkt_ticker] = {
                                    'no_bid': market.get('no_bid', 0),
                                    'no_ask': market.get('no_ask', 0)
                                }
                except:
                    pass
    
    return market_data


def lambda_handler(event, context):
    """Main Lambda handler."""
    et_time = get_et_time()
    next_hour = (et_time + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minutes_to_settlement = int((next_hour - et_time).total_seconds() / 60)
    
    # Get current event prefix
    next_hour_dt = et_time + timedelta(hours=1)
    year = next_hour_dt.strftime('%y')
    month = next_hour_dt.strftime('%b').upper()
    day = next_hour_dt.strftime('%d')
    hour = next_hour_dt.strftime('%H')
    current_event_prefix = f"KXBTCD-{year}{month}{day}{hour}"
    
    # Gather data
    btc_price = get_btc_price()
    
    # Compute volatility by window ONCE (for chart and fair value calculations)
    volatility_by_window = get_volatility_by_window()
    
    # Extract the scaled volatility for the current minutes_to_settlement
    # Clamp to valid range (2-60)
    vol_window = max(2, min(60, minutes_to_settlement))
    vol_std = next((v['volatility'] for v in volatility_by_window if v['window'] == vol_window), 0.10)
    print(f"[DEBUG] Using {vol_window}m volatility: {vol_std:.4f}%")
    
    positions = get_open_positions(current_event_prefix)
    
    # Get market data for positions
    tickers = [p['ticker'] for p in positions]
    market_data = get_market_data(tickers)
    
    # Build open positions with P&L
    open_positions = []
    total_exposure = 0
    
    for pos in positions:
        ticker = pos['ticker']
        contracts = pos['contracts']
        entry_price = pos['price_cents']
        strike = pos['strike_price']
        
        exposure = contracts * entry_price / 100
        total_exposure += exposure
        
        current_data = market_data.get(ticker, {})
        current_bid = current_data.get('no_bid', 0)
        
        # Calculate model fair
        model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_to_settlement)
        
        # Calculate P&L
        entry_cost = contracts * entry_price / 100
        entry_fee = calculate_kalshi_fee(contracts, entry_price)
        
        if current_bid > 0:
            proceeds = contracts * current_bid / 100
            exit_fee = calculate_kalshi_fee(contracts, current_bid)
            unrealized_pnl = proceeds - entry_cost - entry_fee - exit_fee
        else:
            unrealized_pnl = -entry_fee
        
        pnl_pct = (unrealized_pnl / entry_cost) * 100 if entry_cost > 0 else 0
        
        open_positions.append({
            'ticker': ticker,
            'contracts': contracts,
            'price_cents': int(entry_price),
            'exit_price': current_bid,
            'model_fair': model_fair,
            'strike': strike,
            'unrealized_pnl': round(unrealized_pnl, 2),
            'pnl_pct': round(pnl_pct, 1),
            'status': 'ACTIVE'
        })
    
    # Get fair values
    fair_values = get_fair_values(btc_price, current_event_prefix, vol_std, minutes_to_settlement)
    
    # Get trade history for P&L
    trade_history = get_trade_history()
    
    # Calculate unrealized P&L from open positions
    unrealized_pnl = sum(p.get('unrealized_pnl', 0) for p in open_positions)
    
    # Calculate balance - real from Kalshi if live, simulated if dry-run
    if DRY_RUN:
        balance = STARTING_BALANCE + trade_history['total_pnl'] - total_exposure
    else:
        balance = get_real_kalshi_balance()
    
    # Build status data
    data = {
        'btc_price': btc_price,
        'volatility': vol_std,
        'balance': round(balance, 2),
        'settlement_time': next_hour.strftime("%I:%M %p ET"),
        'minutes_to_settlement': minutes_to_settlement,
        'open_positions': open_positions,
        'total_exposure': round(total_exposure, 2),
        'fair_values': fair_values,
        'hourly_summary': {'trades': trade_history['trade_count'], 'avg_edge': 0},
        'closed_trades': trade_history['closed_trades'],
        'pnl': {
            'total': round(trade_history['total_pnl'] + unrealized_pnl, 2),
            'realized': round(trade_history['total_pnl'], 2),
            'unrealized': round(unrealized_pnl, 2),
            'total_fees': round(trade_history['total_fees'], 2),
            'closed_trades': trade_history['trade_count']
        },
        'volatility_by_window': volatility_by_window,  # Already computed above
        'last_updated': datetime.now(timezone.utc).isoformat()
    }


    
    # Upload to S3 with no-cache headers
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=S3_KEY,
            Body=json.dumps(data, indent=2),
            ContentType='application/json',
            CacheControl='no-cache, no-store, must-revalidate'
        )
        print(f"✅ Uploaded status.json to s3://{S3_BUCKET}/{S3_KEY}")
    except Exception as e:
        print(f"Error uploading to S3: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'status': 'success',
            'btc_price': btc_price,
            'positions': len(open_positions),
            'exposure': total_exposure
        })
    }
