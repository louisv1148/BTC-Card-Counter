#!/usr/bin/env python3
"""
Simple HTTP server for trading dashboard
Serves the HTML UI and provides JSON API endpoint
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import sqlite3
from datetime import datetime, timedelta
import os

DB_PATH = "hf_trades.db"

class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            data = self.get_status_data()
            self.wfile.write(json.dumps(data).encode())
        else:
            # Serve static files
            if self.path == '/':
                self.path = '/dashboard.html'
            return SimpleHTTPRequestHandler.do_GET(self)
    
    def get_status_data(self):
        """Get current trading status"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get latest BTC price
        cursor.execute("SELECT btc_price FROM price_observations ORDER BY timestamp DESC LIMIT 1")
        row = cursor.fetchone()
        btc_price = row[0] if row else 0
        
        # Calculate settlement time
        now = datetime.utcnow()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        minutes_to_settlement = int((next_hour - now).total_seconds() / 60)
        
        # Get open positions
        cursor.execute("""
            SELECT ticker, contracts, price_cents, edge_pct, strike_price, timestamp
            FROM trades
            WHERE action = 'open'
            AND ticker NOT IN (SELECT ticker FROM trades WHERE action = 'liquidate')
            ORDER BY timestamp DESC
        """)
        
        open_positions = []
        total_exposure = 0
        for row in cursor.fetchall():
            ticker, contracts, price, edge, strike, ts = row
            exposure = contracts * price / 100
            total_exposure += exposure
            open_positions.append({
                'ticker': ticker,
                'contracts': contracts,
                'price_cents': int(price),
                'edge': edge,
                'strike': strike,
                'opened': datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
            })
        
        # Get recent opportunities
        cursor.execute("""
            SELECT ticker, price_cents, edge_pct, strike_price, timestamp
            FROM price_observations
            WHERE edge_pct >= 10
            AND timestamp > datetime('now', '-10 minutes')
            ORDER BY timestamp DESC
            LIMIT 10
        """)
        
        opportunities = []
        for row in cursor.fetchall():
            ticker, price, edge, strike, ts = row
            opportunities.append({
                'ticker': ticker,
                'price': int(price),
                'edge': edge,
                'strike': strike,
                'time': datetime.fromisoformat(ts).strftime("%H:%M:%S")
            })
        
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
        
        conn.close()
        
        return {
            'btc_price': btc_price,
            'settlement_time': next_hour.strftime("%I:%M %p ET"),
            'minutes_to_settlement': minutes_to_settlement,
            'open_positions': open_positions,
            'total_exposure': total_exposure,
            'opportunities': opportunities,
            'hourly_summary': hourly_summary,
            'closed_trades': closed_trades
        }

def run_server(port=8080):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server_address = ('', port)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"\n🚀 Dashboard running at: http://localhost:{port}")
    print(f"   Press Ctrl+C to stop\n")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server()
