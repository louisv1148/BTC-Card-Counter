#!/bin/bash
# EC2 Setup Script for BTC Trading Bot
# Run this on a fresh Amazon Linux 2023 EC2 instance

set -e

echo "=== Installing dependencies ==="
sudo yum update -y
sudo yum install -y python3-pip git

echo "=== Cloning repository ==="
cd /home/ec2-user
git clone https://github.com/louisv1148/BTC-Card-Counter.git
cd BTC-Card-Counter/btc

echo "=== Installing Python packages ==="
pip3 install --user requests boto3

echo "=== Creating systemd service for bot ==="
sudo tee /etc/systemd/system/btc-bot.service > /dev/null <<EOF
[Unit]
Description=BTC Trading Bot (Dry-Run)
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/BTC-Card-Counter/btc
ExecStart=/usr/bin/python3 /home/ec2-user/BTC-Card-Counter/btc/btc_hf_bot.py --dry-run
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "=== Creating systemd service for dashboard generator ==="
sudo tee /etc/systemd/system/btc-dashboard.service > /dev/null <<EOF
[Unit]
Description=BTC Dashboard Generator
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/BTC-Card-Counter/btc
ExecStart=/usr/bin/python3 /home/ec2-user/BTC-Card-Counter/btc/generate_status.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "=== Enabling and starting services ==="
sudo systemctl daemon-reload
sudo systemctl enable btc-bot btc-dashboard
sudo systemctl start btc-bot btc-dashboard

echo "=== Done! ==="
echo ""
echo "Check bot status:     sudo systemctl status btc-bot"
echo "Check dashboard:      sudo systemctl status btc-dashboard"
echo "View bot logs:        sudo journalctl -u btc-bot -f"
echo "View dashboard logs:  sudo journalctl -u btc-dashboard -f"
