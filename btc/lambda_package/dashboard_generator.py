"""
Dashboard Status Generator Lambda
Generates status.json and uploads to S3 for the dashboard.
Replaces the local generate_status.py script.
"""
import json
import math
import boto3
import requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Configuration
S3_BUCKET = "btc-trading-dashboard-1765598917"
S3_KEY = "status.json"
DYNAMODB_POSITIONS_TABLE = "BTCHFPositions-DryRun"
DYNAMODB_VOL_TABLE = "BTCPriceHistory"
KALSHI_FEE_RATE = 0.07

# Initialize AWS clients
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')


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
    """Calculate model fair value for NO contract."""
    if minutes_left <= 0 or vol_std <= 0:
        return 100 if btc_price < strike else 0
    
    vol_scaled = vol_std * math.sqrt(minutes_left / 15)
    price_diff_pct = (strike - btc_price) / btc_price * 100 if btc_price > 0 else 0
    std_devs = price_diff_pct / vol_scaled if vol_scaled > 0 else 0
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
                    'strike': strike,
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
    vol_std = get_volatility()
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
    
    
    # Calculate balance (starting balance minus current exposure)
    starting_balance = 200.0
    balance = starting_balance - total_exposure
    
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
        'hourly_summary': {'trades': 0, 'avg_edge': 0},
        'closed_trades': [],
        'pnl': {'today': 0, 'week': 0, 'month': 0, 'all_time': 0, 'total': 0, 'realized': 0, 'unrealized': 0, 'total_fees': 0, 'closed_trades': 0},
        'last_updated': datetime.now(timezone.utc).isoformat()
    }

    
    # Upload to S3
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=S3_KEY,
            Body=json.dumps(data, indent=2),
            ContentType='application/json'
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
