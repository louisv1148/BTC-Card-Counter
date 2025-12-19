#!/usr/bin/env python3
"""
Plot theta decay for BTC NO contracts.
Shows how fair value changes as settlement approaches.
"""
import math
import matplotlib.pyplot as plt
import numpy as np

def norm_cdf(z):
    """Normal CDF approximation."""
    if z < -6: return 0.0
    if z > 6: return 1.0
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p

def calculate_fair_value(btc_price, strike, vol_std, minutes_to_settlement):
    """Calculate NO fair value using the model."""
    if minutes_to_settlement <= 0:
        return 100 if btc_price < strike else 0
    
    vol_scaled = vol_std * math.sqrt(minutes_to_settlement / 15)
    price_diff_pct = (strike - btc_price) / btc_price * 100
    std_devs = price_diff_pct / vol_scaled if vol_scaled > 0 else 0
    prob = norm_cdf(std_devs)
    return prob * 100

# Current parameters
btc_price = 88500  # Example BTC price
vol_std = 0.10     # 10% volatility

# Strikes to plot (basis points above spot)
bps_above = [10, 25, 50, 75, 100]  # 0.1%, 0.25%, 0.5%, 0.75%, 1% above

# Time range (60 min down to 0)
minutes = np.arange(60, 0, -1)

plt.figure(figsize=(12, 6))

for bps in bps_above:
    strike = btc_price * (1 + bps / 10000)
    fair_values = [calculate_fair_value(btc_price, strike, vol_std, m) for m in minutes]
    plt.plot(minutes, fair_values, label=f'+{bps}bps (${strike:,.0f})', linewidth=2)

plt.xlabel('Minutes to Settlement', fontsize=12)
plt.ylabel('NO Fair Value (¢)', fontsize=12)
plt.title(f'Theta Decay: NO Fair Value vs Time\nBTC=${btc_price:,} | Vol={vol_std*100:.0f}%', fontsize=14)
plt.legend(title='Strike', loc='lower right')
plt.grid(True, alpha=0.3)
plt.xlim(60, 0)
plt.ylim(0, 100)

# Add horizontal lines for reference
plt.axhline(y=90, color='g', linestyle='--', alpha=0.5, label='90% (hold for win)')
plt.axhline(y=50, color='gray', linestyle='--', alpha=0.5)

plt.tight_layout()
plt.savefig('theta_decay.png', dpi=150)
print("Saved: theta_decay.png")
plt.show()
