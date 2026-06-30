#!/bin/bash
# Run this ONCE on the fresh Oracle Cloud server (as the 'ubuntu' user) to set up the bot.
# Usage: bash setup_server.sh

set -e

echo "=== 1. Updating system packages ==="
sudo apt update && sudo apt upgrade -y

echo "=== 2. Installing Python 3, pip, venv ==="
sudo apt install -y python3 python3-pip python3-venv

echo "=== 3. Creating project directory ==="
mkdir -p ~/tradingsignalbot
cd ~/tradingsignalbot

echo "=== 4. Creating virtual environment ==="
python3 -m venv venv
source venv/bin/activate

echo "=== 5. Installing Python dependencies ==="
# requirements.txt must already be uploaded to this folder (see DEPLOY.md)
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 6. Installing systemd service ==="
sudo cp deploy/nifty-bot.service /etc/systemd/system/nifty-bot.service

# Point the service at the venv's python instead of system python3
sudo sed -i "s|/usr/bin/python3|/home/ubuntu/tradingsignalbot/venv/bin/python3|" /etc/systemd/system/nifty-bot.service

sudo systemctl daemon-reload
sudo systemctl enable nifty-bot.service

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Make sure .env is uploaded to ~/tradingsignalbot/.env"
echo "  2. Start the bot:   sudo systemctl start nifty-bot"
echo "  3. Check status:    sudo systemctl status nifty-bot"
echo "  4. View live logs:  tail -f ~/tradingsignalbot/bot.log"
echo "  5. Stop the bot:    sudo systemctl stop nifty-bot"
