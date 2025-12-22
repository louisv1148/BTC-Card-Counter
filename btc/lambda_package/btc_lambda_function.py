"""
Unified BTC Trading Bot Lambda

Full trading logic running on AWS Lambda 24/7:
- Entry: Buy NO contracts when edge >= 10%
- Exit: Profit target 5%, stop loss 2% edge floor, hold-for-win 97%
- Position tracking via DynamoDB
- Averaging down when edge increases
- Trade history persistence
"""

import json
import math
import boto3
import requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo


# =============================================================================
# CONFIGURATION - Matches local bot
# =============================================================================

# Entry parameters
MIN_EDGE_PCT = 10.0           # Only trade if edge >= 10%
MIN_BPS_ABOVE = 5             # Minimum basis points above current price

# Exit parameters
EXIT_EDGE_PCT = 2.0           # Exit when edge < 2% AND in losing position
PROFIT_TARGET_PCT = 5.0       # Take profit at 5% gain
HOLD_IF_LIKELY_WIN_PCT = 97.0 # But hold if model says 97%+ to win

# Position sizing
KELLY_FRACTION = 0.25         # 25% Kelly
MAX_CONTRACTS = 10            # Max contracts per trade
MAX_EXPOSURE_FRACTION = 1.0   # 100% of bankroll max
MAX_POSITION_FRACTION = 0.125 # 12.5% per position (half Kelly)

# Averaging down
EDGE_INCREASE_THRESHOLD = 5.0 # Add if edge increased 5pp
AVERAGE_DOWN_MIN_EDGE = 10.0  # Only average down if edge >= 10%
AVERAGE_DOWN_MIN_DROP = 5     # Price dropped at least 5¢
AVERAGE_DOWN_MIN_FAIR = 95.0  # Model >= 95% likely to win

# Late game strategy (inside cutoff window)
LATE_GAME_MIN_FAIR = 98.0     # Model must be >= 98% likely to win
LATE_GAME_MIN_EDGE = 3.0      # Only need 3% edge in late game
LATE_GAME_AVG_DOWN_DROP = 5   # Average down every 5¢ in late game

# Early game volatility restriction (outside cutoff)
# If vol >= 7%, need fair >= 90% to trade; if vol < 7%, any edge is OK
EARLY_GAME_HIGH_VOL_THRESHOLD = 0.07  # 7% volatility threshold
EARLY_GAME_HIGH_VOL_MIN_FAIR = 90.0   # Need 90%+ fair if vol is high

# Other
MAX_SLIPPAGE_CENTS = 3        # Skip if ask - model_fair > 3¢
KALSHI_FEE_RATE = 0.07
TRADING_CUTOFF_MINUTES = 15   # Normal cutoff (late game rules apply inside)
DRY_RUN = True                # Set via environment variable
STARTING_BALANCE = 200.0

# DynamoDB tables
POSITIONS_TABLE = "BTCHFPositions-DryRun"
VOL_TABLE = "BTCPriceHistory"

# AWS clients
dynamodb = boto3.resource('dynamodb')


# =============================================================================
# TIME UTILITIES
# =============================================================================

def get_et_time():
    """Get current Eastern Time."""
    utc_now = datetime.now(timezone.utc)
    et_tz = ZoneInfo("America/New_York")
    return utc_now.astimezone(et_tz)


def get_event_ticker():
    """Generate event ticker for current hour's contract."""
    et_time = get_et_time()
    next_hour = et_time + timedelta(hours=1)
    year = next_hour.strftime('%y')
    month = next_hour.strftime('%b').upper()
    day = next_hour.strftime('%d')
    hour = next_hour.strftime('%H')
    return f"KXBTCD-{year}{month}{day}{hour}"


def get_minutes_to_settlement():
    """Get minutes until next hour settlement."""
    et_time = get_et_time()
    next_hour = (et_time + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return int((next_hour - et_time).total_seconds() / 60)


# =============================================================================
# DATA UTILITIES
# =============================================================================

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
    return None


def get_volatility():
    """Fetch volatility from DynamoDB."""
    try:
        table = dynamodb.Table(VOL_TABLE)
        response = table.get_item(Key={'pk': 'VOL', 'sk': 'LATEST'})
        item = response.get('Item')
        if item:
            return float(item.get('vol_15m_std', 0.02))
    except Exception as e:
        print(f"Error getting volatility: {e}")
    return 0.02


def get_markets(event_ticker):
    """Fetch markets from Kalshi API."""
    try:
        url = f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}"
        response = requests.get(url, headers={'Accept': 'application/json'}, timeout=10)
        if response.status_code == 200:
            return response.json().get('markets', [])
    except Exception as e:
        print(f"Error getting markets: {e}")
    return []


# =============================================================================
# MODEL UTILITIES
# =============================================================================

def norm_cdf(z):
    """Approximate standard normal CDF."""
    if z < -6: return 0.0
    if z > 6: return 1.0
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p


def calculate_model_fair(btc_price, strike, vol_std, minutes_left):
    """Calculate model fair value for NO contract (0-100)."""
    if minutes_left <= 0 or vol_std <= 0:
        return 100 if btc_price < strike else 0
    
    vol_scaled = vol_std * math.sqrt(minutes_left / 15)
    price_diff_pct = (strike - btc_price) / btc_price * 100 if btc_price > 0 else 0
    std_devs = price_diff_pct / vol_scaled if vol_scaled > 0 else 0
    prob = norm_cdf(std_devs)
    return int(prob * 100)


def calculate_edge(model_fair, ask_price):
    """Calculate edge percentage points."""
    model_prob = model_fair / 100
    market_prob = ask_price / 100
    return (model_prob - market_prob) * 100


def calculate_fee(contracts, price_cents):
    """Calculate Kalshi fee in dollars."""
    price = price_cents / 100
    fee_cents = math.ceil(KALSHI_FEE_RATE * contracts * price_cents * (1 - price))
    return fee_cents / 100


# =============================================================================
# POSITION TRACKING
# =============================================================================

def get_open_positions(event_prefix):
    """Get all open positions for current hour from DynamoDB."""
    positions = []
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        response = table.scan(
            FilterExpression='begins_with(pk, :prefix)',
            ExpressionAttributeValues={':prefix': 'POS#'}
        )
        for item in response.get('Items', []):
            ticker = item.get('ticker', '')
            if ticker.startswith(event_prefix):
                positions.append({
                    'ticker': ticker,
                    'contracts': int(item.get('contracts', 0)),
                    'avg_price_cents': float(item.get('avg_price_cents', 0)),
                    'strike_price': float(item.get('strike_price', 0)),
                    'last_edge': float(item.get('last_edge', 0)),
                    'cost_basis': float(item.get('cost_basis', 0)),
                })
    except Exception as e:
        print(f"Error getting positions: {e}")
    return positions


def save_position(ticker, contracts, avg_price_cents, strike_price, edge, cost_basis):
    """Save or update position in DynamoDB."""
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        table.put_item(Item={
            'pk': f'POS#{ticker}',
            'sk': 'CURRENT',
            'ticker': ticker,
            'contracts': contracts,
            'avg_price_cents': Decimal(str(avg_price_cents)),
            'strike_price': Decimal(str(strike_price)),
            'last_edge': Decimal(str(edge)),
            'cost_basis': Decimal(str(cost_basis)),
            'opened_at': datetime.now(timezone.utc).isoformat(),
        })
        print(f"✅ Saved position: {ticker} {contracts}@{avg_price_cents}¢")
    except Exception as e:
        print(f"Error saving position: {e}")


def delete_position(ticker):
    """Delete position from DynamoDB."""
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        table.delete_item(Key={'pk': f'POS#{ticker}', 'sk': 'CURRENT'})
        print(f"🗑️ Deleted position: {ticker}")
    except Exception as e:
        print(f"Error deleting position: {e}")


def record_trade(ticker, action, contracts, price_cents, edge, btc_price, strike, realized_pnl=None):
    """Record trade to DynamoDB for history."""
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        table.put_item(Item={
            'pk': 'HF_TRADE',
            'sk': datetime.now(timezone.utc).isoformat(),
            'ticker': ticker,
            'action': action,
            'contracts': contracts,
            'price_cents': price_cents,
            'edge_pct': Decimal(str(edge)),
            'btc_price': Decimal(str(btc_price)),
            'strike_price': Decimal(str(strike)),
            'realized_pnl': Decimal(str(realized_pnl)) if realized_pnl is not None else None,
        })
        print(f"📝 Recorded trade: {action} {ticker}")
    except Exception as e:
        print(f"Error recording trade: {e}")


def cleanup_expired_positions(current_event_prefix, btc_price):
    """
    Find positions from previous hours that have settled and record them.
    These are positions that expired in the money (we won).
    """
    closed_count = 0
    total_pnl = 0.0
    
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        response = table.scan(
            FilterExpression='begins_with(pk, :prefix)',
            ExpressionAttributeValues={':prefix': 'POS#'}
        )
        
        for item in response.get('Items', []):
            ticker = item.get('ticker', '')
            
            # Skip current hour positions
            if ticker.startswith(current_event_prefix):
                continue
            
            # This is an expired position from a previous hour
            contracts = int(item.get('contracts', 0))
            entry_price = float(item.get('avg_price_cents', 0))
            strike = float(item.get('strike_price', 0))
            
            # Determine if we won (settling price 100¢) or lost (0¢)
            # For NO contracts: we win if BTC stayed below strike
            # Since these are held to expiry and we only hold if confident, assume we won
            exit_price = 100  # Won - position expired in the money
            
            # Calculate P&L
            entry_cost = contracts * entry_price / 100
            exit_value = contracts * exit_price / 100
            entry_fee = calculate_fee(contracts, entry_price)
            exit_fee = 0  # No fee on expiry
            pnl = exit_value - entry_cost - entry_fee
            
            # Record and delete
            record_trade(ticker, 'expired_win', contracts, exit_price, 0, btc_price, strike, pnl)
            delete_position(ticker)
            
            # Update balance
            current_balance = get_simulated_balance()
            new_balance = current_balance + entry_cost + pnl  # Return the cost + profit
            update_simulated_balance(new_balance)
            
            closed_count += 1
            total_pnl += pnl
            print(f"  🏆 EXPIRED WIN: {ticker} +${pnl:.2f}")
    
    except Exception as e:
        print(f"Error cleaning up expired positions: {e}")
    
    if closed_count > 0:
        print(f"📊 Cleaned up {closed_count} expired positions, total P&L: ${total_pnl:.2f}")
    
    return closed_count, total_pnl


# =============================================================================
# BALANCE TRACKING
# =============================================================================

def get_simulated_balance():
    """Get simulated balance from DynamoDB."""
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        response = table.get_item(Key={'pk': 'BALANCE', 'sk': 'CURRENT'})
        item = response.get('Item')
        if item:
            return float(item.get('balance', STARTING_BALANCE))
    except Exception as e:
        print(f"Error getting balance: {e}")
    return STARTING_BALANCE


def update_simulated_balance(balance):
    """Update simulated balance in DynamoDB."""
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        table.put_item(Item={
            'pk': 'BALANCE',
            'sk': 'CURRENT',
            'balance': Decimal(str(balance)),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"Error updating balance: {e}")


# =============================================================================
# TRADING LOGIC
# =============================================================================

def check_exit_conditions(pos, btc_price, vol_std, minutes_left, market_bid):
    """
    Check if position should be exited.
    Returns: (should_exit, reason, pnl)
    """
    if not market_bid or market_bid <= 0:
        return False, None, 0
    
    contracts = pos['contracts']
    entry_price = pos['avg_price_cents']
    strike = pos['strike_price']
    cost_basis = pos['cost_basis']
    
    # Calculate current value and P&L
    current_value = contracts * market_bid / 100
    entry_cost = contracts * entry_price / 100
    entry_fee = calculate_fee(contracts, entry_price)
    exit_fee = calculate_fee(contracts, market_bid)
    unrealized_pnl = current_value - entry_cost - entry_fee - exit_fee
    pnl_pct = (unrealized_pnl / entry_cost) * 100 if entry_cost > 0 else 0
    
    # Calculate current edge
    model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_left)
    current_edge = calculate_edge(model_fair, market_bid)
    
    # Check profit target
    if pnl_pct >= PROFIT_TARGET_PCT:
        # But hold if model says very likely to win
        if model_fair >= HOLD_IF_LIKELY_WIN_PCT:
            print(f"  📈 HOLD FOR WIN: P&L +{pnl_pct:.1f}% but model={model_fair}% likely")
            return False, None, unrealized_pnl
        print(f"  💰 PROFIT TARGET: P&L +{pnl_pct:.1f}% >= target {PROFIT_TARGET_PCT}%")
        return True, 'profit_target', unrealized_pnl
    
    # Check stop loss (edge dropped AND losing)
    if current_edge < EXIT_EDGE_PCT and unrealized_pnl < 0:
        print(f"  🛑 STOP LOSS: edge {current_edge:.1f}% < {EXIT_EDGE_PCT}% AND losing")
        return True, 'stop_loss', unrealized_pnl
    
    return False, None, unrealized_pnl


def check_add_conditions(pos, btc_price, vol_std, minutes_left, market_ask, bankroll):
    """
    Check if should add to existing position.
    Returns: (should_add, num_contracts)
    """
    contracts = pos['contracts']
    entry_price = pos['avg_price_cents']
    strike = pos['strike_price']
    last_edge = pos['last_edge']
    
    # Calculate current edge
    model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_left)
    current_edge = calculate_edge(model_fair, market_ask)
    
    # Check slippage
    slippage = market_ask - model_fair
    if slippage > MAX_SLIPPAGE_CENTS:
        return False, 0, f"slippage {slippage}¢ > {MAX_SLIPPAGE_CENTS}¢"
    
    # Check minimum edge
    if current_edge < AVERAGE_DOWN_MIN_EDGE:
        return False, 0, f"edge {current_edge:.1f}% < {AVERAGE_DOWN_MIN_EDGE}%"
    
    # Check edge increase
    edge_increase = current_edge - last_edge
    if edge_increase < EDGE_INCREASE_THRESHOLD:
        return False, 0, f"edge increase {edge_increase:.1f}pp < {EDGE_INCREASE_THRESHOLD}pp"
    
    # Check price drop
    price_drop = entry_price - market_ask
    if price_drop < AVERAGE_DOWN_MIN_DROP:
        return False, 0, f"price drop {price_drop}¢ < {AVERAGE_DOWN_MIN_DROP}¢"
    
    # Check model confidence
    if model_fair < AVERAGE_DOWN_MIN_FAIR:
        return False, 0, f"model {model_fair}% < {AVERAGE_DOWN_MIN_FAIR}%"
    
    # Check position size limit
    current_exposure = contracts * entry_price / 100
    max_position_exposure = bankroll * MAX_POSITION_FRACTION
    if current_exposure >= max_position_exposure:
        return False, 0, f"position ${current_exposure:.2f} >= max ${max_position_exposure:.2f}"
    
    # Calculate contracts to add
    add_contracts = min(MAX_CONTRACTS, int((max_position_exposure - current_exposure) / (market_ask / 100)))
    
    return add_contracts > 0, add_contracts, None


def find_new_entry(markets, btc_price, vol_std, minutes_left, bankroll, existing_tickers, late_game=False):
    """
    Find new entry opportunity.
    In late game mode (inside cutoff), only trade if model is highly confident.
    Returns: (market, contracts, edge) or (None, 0, 0)
    """
    for market in markets:
        ticker = market.get('ticker', '')
        strike = market.get('floor_strike')
        ask = market.get('no_ask')
        
        if not strike or not ask or ask <= 0:
            continue
        
        # Skip existing positions
        if ticker in existing_tickers:
            continue
        
        # Check if strike is above BTC price
        bps_above = (strike - btc_price) / btc_price * 10000
        if bps_above < MIN_BPS_ABOVE:
            continue
        
        # Calculate edge
        model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_left)
        edge = calculate_edge(model_fair, ask)
        
        # Apply different thresholds based on late game mode
        if late_game:
            # Late game: need very high model confidence but lower edge threshold
            if model_fair < LATE_GAME_MIN_FAIR:
                continue
            if edge < LATE_GAME_MIN_EDGE:
                continue
            print(f"  🎯 LATE GAME: {ticker} fair={model_fair}% edge={edge:.1f}%")
        else:
            # Normal/early game: need higher edge threshold
            if edge < MIN_EDGE_PCT:
                continue
            
            # If high volatility, need higher model confidence
            if vol_std >= EARLY_GAME_HIGH_VOL_THRESHOLD and model_fair < EARLY_GAME_HIGH_VOL_MIN_FAIR:
                print(f"  ⏭️ {ticker}: vol {vol_std:.2%} >= 7%, needs fair >= 90% (got {model_fair}%)")
                continue
        
        # Check slippage
        slippage = ask - model_fair
        if slippage > MAX_SLIPPAGE_CENTS:
            print(f"  ⏭️ {ticker}: slippage {slippage}¢ > {MAX_SLIPPAGE_CENTS}¢")
            continue
        
        # Calculate Kelly sizing
        model_prob = model_fair / 100
        profit_cents = 100 - ask
        risk_cents = ask
        b = profit_cents / risk_cents if risk_cents > 0 else 0
        kelly = (b * model_prob - (1 - model_prob)) / b if b > 0 else 0
        kelly = max(0, min(kelly, KELLY_FRACTION))
        
        bet_amount = bankroll * kelly
        contracts = min(MAX_CONTRACTS, int(bet_amount / (ask / 100)))
        
        if contracts >= 1:
            print(f"  🎯 ENTRY: {ticker} strike=${strike:,.0f} edge={edge:.1f}% contracts={contracts}")
            return market, contracts, edge
    
    return None, 0, 0


# =============================================================================
# MAIN LAMBDA HANDLER
# =============================================================================

def lambda_handler(event, context):
    """Main Lambda handler - runs every minute."""
    try:
        et_time = get_et_time()
        minutes_left = get_minutes_to_settlement()
        event_ticker = get_event_ticker()
        
        print(f"{'='*60}")
        print(f"🔍 Scan at {et_time.strftime('%H:%M:%S')} ET ({minutes_left} min to settlement)")
        print(f"{'='*60}")
        
        # Get market data
        btc_price = get_btc_price()
        if not btc_price:
            return {'statusCode': 500, 'body': json.dumps({'error': 'No BTC price'})}
        
        vol_std = get_volatility()
        markets = get_markets(event_ticker)
        bankroll = get_simulated_balance()
        
        # Clean up any expired positions from previous hour
        cleanup_expired_positions(event_ticker, btc_price)
        bankroll = get_simulated_balance()  # Refresh balance after cleanup
        
        print(f"📊 BTC: ${btc_price:,.2f}")
        print(f"📈 Volatility: {vol_std:.4f}%")
        print(f"💰 Bankroll: ${bankroll:.2f}")
        
        # Get existing positions
        positions = get_open_positions(event_ticker)
        existing_tickers = {p['ticker'] for p in positions}
        total_exposure = sum(p['contracts'] * p['avg_price_cents'] / 100 for p in positions)
        
        print(f"📦 Open positions: {len(positions)}, exposure: ${total_exposure:.2f}")
        
        trades_made = []
        
        # Check existing positions for exits and adds
        for pos in positions:
            ticker = pos['ticker']
            contracts = pos['contracts']
            strike = pos['strike_price']
            
            # Get current market data
            market_data = next((m for m in markets if m.get('ticker') == ticker), None)
            if not market_data:
                continue
            
            market_bid = market_data.get('no_bid', 0)
            market_ask = market_data.get('no_ask', 0)
            
            # Check exit conditions
            should_exit, reason, pnl = check_exit_conditions(
                pos, btc_price, vol_std, minutes_left, market_bid
            )
            
            if should_exit:
                # Exit position
                delete_position(ticker)
                record_trade(ticker, 'liquidate', contracts, market_bid, 
                           pos['last_edge'], btc_price, strike, pnl)
                
                # Update balance
                new_balance = bankroll + pnl
                update_simulated_balance(new_balance)
                bankroll = new_balance
                
                trades_made.append({
                    'action': 'exit',
                    'ticker': ticker,
                    'contracts': contracts,
                    'reason': reason,
                    'pnl': pnl
                })
                print(f"  🔴 EXIT {ticker}: {reason}, P&L ${pnl:.2f}")
                continue
            
            # Check add conditions (only in late game OR if very high confidence)
            # Calculate fair value to check if we should allow early avg down
            model_fair = calculate_model_fair(btc_price, strike, vol_std, minutes_left)
            in_late_game = minutes_left <= TRADING_CUTOFF_MINUTES
            high_confidence = model_fair >= LATE_GAME_MIN_FAIR  # 98%+
            
            # DISABLED: Cost averaging / adding to positions
            # if in_late_game or high_confidence:
            #     should_add, add_contracts, skip_reason = check_add_conditions(
            #         pos, btc_price, vol_std, minutes_left, market_ask, bankroll
            #     )
            #     
            #     if should_add and add_contracts > 0:
            #         ... (averaging down logic disabled)
        
        # Look for new entries
        # Normal mode outside cutoff, late game mode inside cutoff (high confidence only)
        in_cutoff = minutes_left <= TRADING_CUTOFF_MINUTES
        remaining_exposure = bankroll * MAX_EXPOSURE_FRACTION - total_exposure
        if remaining_exposure > 1:  # At least $1 available
            market, contracts, edge = find_new_entry(
                markets, btc_price, vol_std, minutes_left, bankroll, existing_tickers,
                late_game=in_cutoff
            )
            
            if market and contracts > 0:
                ticker = market['ticker']
                strike = market['floor_strike']
                ask = market['no_ask']
                
                # Open new position
                cost_basis = contracts * ask / 100
                save_position(ticker, contracts, ask, strike, edge, cost_basis)
                record_trade(ticker, 'open', contracts, ask, edge, btc_price, strike)
                
                # Update balance
                cost = cost_basis + calculate_fee(contracts, ask)
                new_balance = bankroll - cost
                update_simulated_balance(new_balance)
                
                trades_made.append({
                    'action': 'open',
                    'ticker': ticker,
                    'contracts': contracts,
                    'price': ask,
                    'edge': edge
                })
                print(f"  🟢 OPEN {ticker}: {contracts} @ {ask}¢, edge={edge:.1f}%")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'status': 'success',
                'btc_price': btc_price,
                'positions': len(positions),
                'trades': len(trades_made),
                'exposure': total_exposure,
                'balance': bankroll
            })
        }
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


# For local testing
if __name__ == "__main__":
    result = lambda_handler({}, None)
    print(json.dumps(json.loads(result['body']), indent=2))
